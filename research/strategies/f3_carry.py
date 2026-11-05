"""F3: funding carry, long spot and short perp, daily. research/STRATEGY_SPEC.md defines it.

Flagged everywhere it's reported: UK retail clients can't use crypto derivatives, so this family may be unavailable to
the owner whatever it shows, and liquidation isn't modelled.

Where the spec leaves a choice open, these are the readings taken:

- Positions are decided from the close of the bar before F3_START_TS on, and nothing is held before it. Run from the
  panel's first row, a TOP5 position opened in March 2020, when the mask held 1 to 4 names, would carry a sleeve of up
  to 1 into the window, sized on a universe Amendment 5 says can't be traded yet. Which way that moves a result can't
  be known in advance.
- m's window of D rows ignores NaN, as the spec says, and a row before the panel's first counts as one more NaN.
- The shared "no data at t" rule covers both legs, because the position holds the spot asset as well as the perp. So
  a held position closes on a row where the perp's close or its spot's close is NaN, and nothing opens there. A perp
  row with no spot row on that day, or a perp with no spot column of its name, counts as a NaN spot close. It opens
  again only when m > E again, with that day's sleeve. A NaN m alone neither opens nor closes.
- The spot column is found by the perp's name, as backtest_carry pairs the legs. That reads a name only to pair the two
  legs of one asset, never to decide which assets to hold.
- An opening that has to wait for room keeps waiting, as the spec says, until there's room. It stops waiting where a
  held position would close (m < 0, out of the mask, or no bar on either leg), and m falling to between 0 and E
  doesn't stop it. When room appears, it opens with that row's sleeve. Waiting names go first, the longest waiting
  first, then new openings, each group in column order, which is name order in clean.py's panels. Each goes in only
  while the row's sum stays at or below 1 (+1e-9, the engine's tolerance).
- E's neighbours are 4% and 6% for a 5% threshold, and the exit threshold of 0 isn't varied, as the spec says.
"""
from __future__ import annotations

import numpy as np

from research.harness.data import top_n_mask
from research.harness.engine import backtest_carry
from research.strategies.common import F3_START_TS, DAY, check_spacing, column, from_start, rolling_sum

FAMILY = "F3"
PANELS = ("spot_1d", "perps_1d")
UNIVERSES = ({"universe": "BTC"}, {"universe": "BTC_ETH"}, {"universe": "TOP", "n": 5})
_ROW_TOLERANCE = 1e-9
NUMERIC = {"D": {"integer": True}, "E": {"integer": False}, "n": {"integer": True}}


def base_variants() -> list[dict]:
    """The 27 grid points: universe, funding window D, entry threshold E, each in the spec's order."""
    return [{**universe, "D": days, "E": threshold} for universe in UNIVERSES for days in (3, 7, 30)
            for threshold in (0.05, 0.10, 0.20)]


def parts(params: dict) -> list[str]:
    """The id's parts, such as ["TOP5", "D7", "E10"]."""
    universe = f"TOP{params['n']}" if params["universe"] == "TOP" else params["universe"]
    return [universe, f"D{params['D']}", f"E{round(params['E'] * 100)}"]


def funding_mean(funding: np.ndarray, days: int) -> np.ndarray:
    """m[t, a]: the mean of funding_sum over the D rows ending at t, ignoring NaN, needing one value, times 365."""
    padded = np.vstack([np.full((days - 1, funding.shape[1]), np.nan), funding])
    total = rolling_sum(np.nan_to_num(padded, nan=0.0), days)[days - 1 :]
    count = rolling_sum((~np.isnan(padded)).astype(np.float64), days)[days - 1 :]
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(count > 0, total / count, np.nan) * 365


def _spot_closes(perps, spot, names: np.ndarray) -> np.ndarray:
    """Each of these perp columns' spot closes, on the perp panel's rows. NaN where spot has no such day or name."""
    out = np.full((perps.ts.size, names.size), np.nan)
    rows = np.searchsorted(spot.ts, perps.ts)
    found = rows < spot.ts.size
    found[found] = spot.ts[rows[found]] == perps.ts[found]
    for k, a in enumerate(names.tolist()):
        if perps.assets[a] in spot.assets:
            out[found, k] = spot.close[rows[found], spot.assets.index(perps.assets[a])]
    return out


def sleeves(params: dict, perps, spot) -> np.ndarray:
    """T x A sleeves on the perp panel's rows and columns. A sleeve is fixed on opening and held until it closes."""
    check_spacing(perps, DAY)
    rows, columns = perps.close.shape
    if params["universe"] == "TOP":
        mask = top_n_mask(perps, params["n"])
        names = np.flatnonzero(mask.any(axis=0))
        inside = mask[:, names]
        size = np.where(mask.sum(axis=1) > 0, 1.0 / np.maximum(mask.sum(axis=1), 1), 0.0)
    else:
        names = np.array([column(perps, "BTC")] if params["universe"] == "BTC"
                         else [column(perps, "BTC"), column(perps, "ETH")])
        inside = np.ones((rows, names.size), dtype=bool)
        size = np.full(rows, 1.0 / names.size)
    m = funding_mean(perps.extras["funding_sum"][:, names], params["D"])
    has_bar = ~np.isnan(perps.close[:, names]) & ~np.isnan(_spot_closes(perps, spot, names))
    deciding = from_start(perps.ts, F3_START_TS)

    out = np.zeros((rows, columns))
    held = np.zeros(names.size)
    waiting = np.full(names.size, -1)  # The row a name started waiting for room on, or -1.
    for t in np.flatnonzero(deciding):
        with np.errstate(invalid="ignore"):
            closing = (m[t] < 0) | ~has_bar[t] | ~inside[t]
            held[closing] = 0.0
            waiting[closing] = -1
            waiting[(held == 0) & (waiting < 0) & (m[t] > params["E"]) & has_bar[t] & inside[t]] = t
        for i in sorted(np.flatnonzero(waiting >= 0).tolist(), key=lambda i: (waiting[i], i)):
            if held.sum() + size[t] > 1 + _ROW_TOLERANCE:
                break  # Every opening on a row has the same sleeve, so none after this one fits either.
            held[i] = size[t]
            waiting[i] = -1
        out[t, names] = held
    return out


def universe_mask(params: dict, data: dict):
    """TOP's mask on the perp panel, for random_carry_spells. None for BTC and BTC_ETH."""
    return top_n_mask(data["perps"], params["n"]) if params["universe"] == "TOP" else None


def run(params: dict, data: dict, cost_model, fee_level: str, cost_scale: float = 1.0, fee_bps=None):
    perps = data["perps"]
    return backtest_carry(data["spot"], perps, sleeves(params, perps, data["spot"]), name="__".join([FAMILY, *parts(params)]),
                          start_ts=F3_START_TS, cost_model=cost_model, fee_level=fee_level, cost_scale=cost_scale,
                          fee_bps=fee_bps)
