"""The panels strategies actually use: stored panels with the rules of Amendment 5 of the preregistration applied.

Why a stored column can't be used as it is:

- A gap can hide a different asset. The LUNA column holds two coins, and reading across its gap shows a gain of tens
  of thousands of times that never existed. So a break ends the asset, and whatever follows is a new listing in its own
  column.
- A column can hide a fork. BCC stops as one coin and comes back for a day at its old price with nothing traded, while a
  holder really had one BCHABC and one BCHSV, worth much less. fix_bcc_fork() prices that.
- Perps and spot name the same coin differently, and a 1000PEPE perp is priced per thousand coins. A plain rename
  would make the short leg's P&L 1000x too big without a word.
- The archive keeps publishing funding for contracts that have stopped trading, and nobody received those payments.
- A ticker can be reused on holdout dates, by something priced differently. backtest_carry sizes units from spot, so a
  perp at 4 times spot makes a short leg 4 times the long one, with funding 4 times too large. So a kept contract is
  checked row by row there, and ends where it stops looking like its spot.

These run when a panel is loaded, so holdout prices are only looked at after unlock() has written its ledger line.
"""
from __future__ import annotations

import re

import numpy as np

from research.harness.data import CORE_FIELDS, HOLDOUT_START_TS, Panel, load_panel
from research.harness.engine import DELIST_HAIRCUT

MAX_GAP_DAYS = 30
MAX_JUMP = 5.0
_SECONDS_PER_DAY = 86400
_RENAMED_PERPS = {"LUNA2": "LUNA~2", "DODOX": "DODO"}  # Amendment 5. The new LUNA is the second part of spot's column.
_PREFIX = re.compile(r"^(1000000|100000|10000|1000|1M)(.+)$")
_PREFIX_SCALE = {"1000": 1e3, "10000": 1e4, "100000": 1e5, "1000000": 1e6, "1M": 1e6}
_MATCH_LOW, _MATCH_HIGH = 0.8, 1.25  # A perp and its spot must be this close, or the names only look alike.
_HOLDOUT_WINDOW = 7  # Shared rows behind each holdout row's price check.
_BCC_FORK_TS = 1542326400  # 2018-11-16, the first day BCHABC and BCHSV trade and the first day BCC doesn't.
_BCC_STALE_TS = 1542672000  # 2018-11-20, a bar with no volume at BCC's last price from before the fork.


def _fields(panel: Panel) -> dict:
    return {**{key: getattr(panel, key) for key in CORE_FIELDS}, **panel.extras}


def _rebuilt(panel: Panel, assets: list[str], fields: dict, meta: dict) -> Panel:
    order = sorted(range(len(assets)), key=assets.__getitem__)
    names = [assets[column] for column in order]
    blocks = {key: values[:, order] for key, values in fields.items()}
    extras = {key: values for key, values in blocks.items() if key not in CORE_FIELDS}
    return Panel(panel.name, panel.ts, tuple(names), *(blocks[key] for key in CORE_FIELDS), extras=extras, meta=meta)


def split_breaks(panel: Panel, max_gap_days: int = MAX_GAP_DAYS, max_jump: float = MAX_JUMP) -> Panel:
    """Ends an asset at each break, and gives what follows its own column, named X~2, X~3 and so on.

    A break is a gap in bars of more than max_gap_days, or a gap of any length where the first open after it is at
    least max_jump times, or at most 1/max_jump of, the last close before it. A shorter, quieter gap stays one asset.
    Rows outside a part are NaN in that part's column, so the first part ends in a real delisting.

    A gap is in days, so a stretch with no rows at all is one as much as rows with no bar for this asset.
    """
    source = _fields(panel)
    columns, names, splits = [], [], []
    for column, asset in enumerate(panel.assets):
        bars = np.flatnonzero(~np.isnan(panel.close[:, column]))
        cuts = []
        for last, first in zip(bars[:-1], bars[1:]):
            days = (panel.ts[first] - panel.ts[last]) / _SECONDS_PER_DAY
            if first == last + 1 and days <= 1:
                continue
            ratio = panel.open[first, column] / panel.close[last, column]
            if days > max_gap_days or ratio >= max_jump or ratio <= 1 / max_jump:
                cuts.append(first)
                splits.append({
                    "asset": asset, "last_bar_ts": int(panel.ts[last]), "first_bar_ts": int(panel.ts[first]),
                    "days_between": float(days), "open_over_last_close": float(ratio),
                })
        if not cuts:
            columns.append((column, 0, panel.ts.size))
            names.append(asset)
            continue
        edges = [0, *cuts, panel.ts.size]
        for part, (start, end) in enumerate(zip(edges[:-1], edges[1:]), start=1):
            columns.append((column, start, end))
            names.append(asset if part == 1 else f"{asset}~{part}")

    fields = {}
    for key, values in source.items():
        block = np.full((panel.ts.size, len(columns)), np.nan)
        for new, (old, start, end) in enumerate(columns):
            block[start:end, new] = values[start:end, old]
        fields[key] = block
    return _rebuilt(panel, names, fields, {**panel.meta, "splits": splits})


def fix_bcc_fork(panel: Panel) -> Panel:
    """Amendment 6: BCC's last bar is 2018-11-16, at what its holder then had, one BCHABC plus one BCHSV.

    The stored column stops on 2018-11-15 at 448.70 and shows that price again on 2018-11-20 with no volume. Read as it
    stands, a holder rides a 20% market fall untouched and leaves far above the 286.51 the two coins were worth that day.

    Nothing traded on this bar, but the engines fill any order on a bar with a price. So its close is the two coins, and
    its open is that less the delisting haircut, the one price at which an order on it can't gain. A seller gets what a
    holder who rides it to the delisting gets, and a buyer gets the haircut back on the day and pays it the next.
    """
    names = {asset: column for column, asset in enumerate(panel.assets)}
    if not {"BCC", "BCHABC", "BCHSV"} <= set(names):
        return panel
    fork = int(np.searchsorted(panel.ts, _BCC_FORK_TS))
    if fork == panel.ts.size or panel.ts[fork] != _BCC_FORK_TS:
        return panel
    fields = {key: values.copy() for key, values in _fields(panel).items()}
    worth = fields["close"][fork, names["BCHABC"]] + fields["close"][fork, names["BCHSV"]]
    for key, price in (("open", worth * (1 - DELIST_HAIRCUT)), ("high", worth), ("low", worth * (1 - DELIST_HAIRCUT)),
                       ("close", worth)):
        fields[key][fork, names["BCC"]] = price
    for key in ("volume", "quote_volume"):  # No BCC traded. Its 30-day median volume is barely moved by one zero.
        fields[key][fork, names["BCC"]] = 0.0
    stale = int(np.searchsorted(panel.ts, _BCC_STALE_TS))
    if stale < panel.ts.size and panel.ts[stale] == _BCC_STALE_TS:
        for key in fields:
            fields[key][stale, names["BCC"]] = np.nan
    return _rebuilt(panel, list(panel.assets), fields, dict(panel.meta))


def spot_panel(split: str = "dev", key=None) -> Panel:
    return split_breaks(fix_bcc_fork(load_panel("spot_1d", split, key)))


def majors_panel(split: str = "dev", key=None) -> Panel:
    """BTC and ETH at 15 minutes. Neither has a break, and resample() makes the longer bars from this."""
    return load_panel("majors_15m", split, key)


def _names_with_development_rows(panel: Panel) -> set[str]:
    """The columns a dev load of this panel holds: data.py's dev file has those with data in its rows, and no others."""
    rows = panel.ts < HOLDOUT_START_TS
    has_data = np.zeros(len(panel.assets), dtype=bool)
    for values in _fields(panel).values():
        has_data |= ~np.isnan(values[rows]).all(axis=0)
    return {asset for asset, kept in zip(panel.assets, has_data) if kept}


def perp_identity(symbol: str, spot_assets) -> tuple[str, float] | None:
    """(spot column name, how many coins one perp unit prices), or None if no spot column matches.

    An exact match wins, because 1000SATS is a coin's own name in spot and isn't priced per thousand.
    """
    base, part = symbol, ""
    if "~" in base:
        base, part = base.split("~", 1)
        part = f"~{part}"
    if not base.endswith("USDT"):
        return None
    base = base[: -len("USDT")]
    if base in _RENAMED_PERPS:
        name = _RENAMED_PERPS[base]
        return (name, 1.0) if not part and name in spot_assets else None
    if f"{base}{part}" in spot_assets:
        return f"{base}{part}", 1.0
    match = _PREFIX.match(base)
    if match and f"{match.group(2)}{part}" in spot_assets:
        return f"{match.group(2)}{part}", _PREFIX_SCALE[match.group(1)]
    return None


def _first_holdout_row_off_spot(ts: np.ndarray, perp_close: np.ndarray, spot_close: np.ndarray):
    """(ts, median) of the first holdout row whose median over the last 7 shared rows is outside the match, or None.

    It only reads rows up to the one it's on, and it can only name a holdout row. A contract with fewer than 7 shared
    rows so far is judged on the ones it has, because waiting would keep a misprice for the first week of a new listing.
    """
    both = ~np.isnan(perp_close) & ~np.isnan(spot_close)
    days, ratios = ts[both], perp_close[both] / spot_close[both]
    first = int(np.searchsorted(days, HOLDOUT_START_TS))
    if first == days.size:
        return None
    padded = np.r_[np.full(_HOLDOUT_WINDOW - 1, np.nan), ratios]
    medians = np.nanmedian(np.lib.stride_tricks.sliding_window_view(padded, _HOLDOUT_WINDOW)[first:], axis=1)
    off = np.flatnonzero((medians < _MATCH_LOW) | (medians > _MATCH_HIGH))
    return (int(days[first + off[0]]), float(medians[off[0]])) if off.size else None


def match_perps(perps: Panel, spot: Panel) -> Panel:
    """Perps under spot's names and units, with breaks split, unmatched ones gone and funding only on traded days.

    Amendment 6: which contracts are kept is settled on development rows wherever a name has any, so that loading the
    holdout can't change what a development date holds. A name with none is settled on the rows it has.

    So a contract with development bars is what a dev load makes of it, in every way. Its identity comes from the spot
    columns with development rows, and its price check from the days it shares with them. One that a dev load leaves out
    stays out on holdout dates too, even after its spot lists: a new listing there has no rule yet, and keeping it would
    put it on development dates, ranked in top_n_mask, on the strength of a listing that came later.

    A tie between two contracts for one coin stays with the first to list, and then the first by name. Going by who has
    more bars would let later rows erase a contract that died, and its delisting, from dates when nobody could know. So
    "the first" isn't by name alone either: a later contract with a name that sorts first would do the same.

    Holdout rows aren't left unchecked. From the first holdout row where the median over the last 7 shared rows is
    outside the match, a kept contract's rows are blanked, which can't change a development row, and the engines see a
    delisting. meta["blanked"] says which and from when.
    """
    split = split_breaks(perps)
    spot_assets = set(spot.assets)
    spot_on_development_rows = _names_with_development_rows(spot)
    spot_index = {asset: column for column, asset in enumerate(spot.assets)}
    source = _fields(split)
    chosen: dict[str, tuple[int, float]] = {}
    left_out = []
    shared = np.intersect1d(split.ts, spot.ts, return_indices=True)
    settling = shared[0] < HOLDOUT_START_TS

    def first_bar(column: int) -> int:
        return int((~np.isnan(split.close[:, column])).argmax())

    for column, symbol in enumerate(split.assets):
        on_development_rows = bool((~np.isnan(split.close[split.ts < HOLDOUT_START_TS, column])).any())
        identity = perp_identity(symbol, spot_on_development_rows if on_development_rows else spot_assets)
        if identity is None:
            left_out.append({"symbol": symbol, "why": "no spot column"})
            continue
        name, scale = identity
        # The prices must agree too. A name alone can pair a coin with something else that took its ticker.
        perp_close = split.close[shared[1], column] / scale
        spot_close = spot.close[shared[2], spot_index[name]]
        both = ~np.isnan(perp_close) & ~np.isnan(spot_close)
        if on_development_rows:
            both &= settling
        if both.any():
            median = float(np.median(perp_close[both] / spot_close[both]))
            if not _MATCH_LOW <= median <= _MATCH_HIGH:
                left_out.append({"symbol": symbol, "why": f"priced {median:.3g} times spot"})
                continue
        else:
            left_out.append({"symbol": symbol, "why": "no day shared with spot"})
            continue
        if name in chosen:
            # Two contracts for one coin: the first to list stays, and the choice is on the record. The columns are in
            # name order, so a tie stays with the one already chosen.
            other = chosen[name][0]
            if first_bar(other) <= first_bar(column):
                left_out.append({"symbol": symbol, "why": f"{name} already matched"})
                continue
            left_out.append({"symbol": split.assets[other], "why": f"{name} already matched"})
        chosen[name] = (column, scale)

    names = sorted(chosen)
    fields = {key: np.empty((split.ts.size, len(names))) for key in source}
    blanked = []
    for new, name in enumerate(names):
        column, scale = chosen[name]
        for key, values in source.items():
            fields[key][:, new] = values[:, column]
        for key in ("open", "high", "low", "close"):
            fields[key][:, new] /= scale
        fields["volume"][:, new] *= scale
        off = _first_holdout_row_off_spot(shared[0], split.close[shared[1], column] / scale,
                                          spot.close[shared[2], spot_index[name]])
        if off is not None:
            blanked.append({"symbol": split.assets[column], "asset": name, "from_ts": off[0], "median": off[1]})
            for key in fields:
                fields[key][split.ts >= off[0], new] = np.nan
    for key in ("funding_sum", "funding_count"):
        if key in fields:
            fields[key][np.isnan(fields["close"])] = np.nan
    return _rebuilt(split, names, fields, {**split.meta, "left_out": left_out, "blanked": blanked})


def perp_panel(spot: Panel, split: str = "dev", key=None) -> Panel:
    return match_perps(load_panel("perps_1d", split, key), spot)


def usd_per_gbp(split: str = "dev", key=None) -> tuple[np.ndarray, np.ndarray]:
    """The BTC-implied rate at each close, from Coinbase's BTC-USD and BTC-GBP. NaN where either has no close."""
    usd, gbp = load_panel("coinbase_1d", split, key), load_panel("coinbase_gbp_1d", split, key)
    usd_column = next(column for column, asset in enumerate(usd.assets) if asset.startswith("BTC"))
    gbp_column = next(column for column, asset in enumerate(gbp.assets) if asset.startswith("BTC"))
    days, usd_rows, gbp_rows = np.intersect1d(usd.ts, gbp.ts, return_indices=True)
    return days, usd.close[usd_rows, usd_column] / gbp.close[gbp_rows, gbp_column]
