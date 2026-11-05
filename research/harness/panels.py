"""Cutting and re-bucketing panels. Each function returns a new Panel and leaves the one it was given alone."""
from __future__ import annotations

import operator

import numpy as np

from research.harness.data import CORE_FIELDS, Panel, _make_panel, _panel_fields


def _meta_at_the_cut(panel: Panel, rows: int) -> dict:
    """panel.meta as far as it was known at the cut. clean.py writes two things into it from rows that may be later.

    A break is only known from its first bar after the gap. Why match_perps left a contract out can rest on any of its
    rows, so a cut holds none of that.
    """
    meta = dict(panel.meta)
    if rows == panel.ts.size:
        return meta
    meta.pop("left_out", None)
    if "splits" in meta:
        meta["splits"] = [entry for entry in meta["splits"] if rows and entry["first_bar_ts"] <= panel.ts[rows - 1]]
    return meta


def truncate(panel: Panel, rows: int) -> Panel:
    """The first `rows` bars, with the same columns even if one is then all NaN.

    The truncation test compares against this, so a strategy sees exactly what it would have seen at that time, apart
    from the column names, which research/STRATEGY_SPEC.md rules out reading.
    """
    rows = operator.index(rows)
    # A negative count would slice from the far end and hand back later bars instead of earlier ones.
    if not 0 <= rows <= panel.ts.size:
        raise ValueError(f"rows must be between 0 and {panel.ts.size}, got {rows}")
    fields = {key: values[:rows] for key, values in _panel_fields(panel).items()}
    return _make_panel(panel.name, panel.ts[:rows], panel.assets, fields, _meta_at_the_cut(panel, rows))


def select_assets(panel: Panel, names) -> Panel:
    """Those columns, in the order given. An unknown name raises ValueError."""
    names = list(names)
    unknown = [name for name in names if name not in panel.assets]
    if unknown:
        raise ValueError(f"{panel.name} has no assets named {unknown}")
    columns = [panel.assets.index(name) for name in names]
    fields = {key: values[:, columns] for key, values in _panel_fields(panel).items()}
    return _make_panel(panel.name, panel.ts, names, fields, dict(panel.meta))


def _first_valid(values: np.ndarray, starts: np.ndarray) -> np.ndarray:
    """Each bucket's first non-NaN value per column, NaN if it has none."""
    rows = values.shape[0]
    index = np.where(np.isnan(values), rows, np.arange(rows)[:, None])
    first = np.minimum.reduceat(index, starts, axis=0)
    return _taken(values, first, first < rows)


def _last_valid(values: np.ndarray, starts: np.ndarray) -> np.ndarray:
    """Each bucket's last non-NaN value per column, NaN if it has none."""
    index = np.where(np.isnan(values), -1, np.arange(values.shape[0])[:, None])
    last = np.maximum.reduceat(index, starts, axis=0)
    return _taken(values, last, last >= 0)


def _taken(values: np.ndarray, rows: np.ndarray, found: np.ndarray) -> np.ndarray:
    out = np.full(rows.shape, np.nan)
    out[found] = values[rows[found], np.nonzero(found)[1]]
    return out


def _sum_valid(values: np.ndarray, starts: np.ndarray) -> np.ndarray:
    """Each bucket's sum per column, ignoring NaN. NaN if it has nothing to add, because no data isn't zero volume."""
    missing = np.isnan(values)
    total = np.add.reduceat(np.where(missing, 0.0, values), starts, axis=0)
    total[np.logical_and.reduceat(missing, starts, axis=0)] = np.nan
    return total


def resample(panel: Panel, minutes: int) -> Panel:
    """Bars of `minutes`, starting on UTC multiples of that length, each field per asset.

    A bucket with no rows is left out, and one with rows but no data for an asset is NaN for it. A trailing
    incomplete bucket is kept, because it can't be told apart from a gap, and it's only known at its last row's
    close. Extras are not carried.
    """
    if minutes != int(minutes) or minutes < 1:
        raise ValueError(f"minutes must be a whole number of 1 or more, got {minutes}")
    seconds = int(minutes) * 60
    buckets = panel.ts // seconds
    starts = np.flatnonzero(np.r_[True, buckets[1:] != buckets[:-1]][: buckets.size])
    if not starts.size:
        fields = {key: getattr(panel, key) for key in CORE_FIELDS}
        return _make_panel(panel.name, panel.ts, panel.assets, fields, dict(panel.meta))
    fields = {
        "open": _first_valid(panel.open, starts),
        "high": np.fmax.reduceat(panel.high, starts, axis=0),  # fmax and fmin skip NaN, and give NaN only if all are.
        "low": np.fmin.reduceat(panel.low, starts, axis=0),
        "close": _last_valid(panel.close, starts),
        "volume": _sum_valid(panel.volume, starts),
        "quote_volume": _sum_valid(panel.quote_volume, starts),
    }
    return _make_panel(panel.name, buckets[starts] * seconds, panel.assets, fields, dict(panel.meta))
