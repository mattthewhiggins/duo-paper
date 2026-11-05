"""Parity: the runner's targets equal the research's, for every one of the last 120 development days.

The research side is research.harness.clean.spot_panel("dev") and the strategy module's own targets(), as the research
computes them. The runner side is what the runner would have done at 00:02 UTC the day after each of those days: the
development data's own rows, served as Binance klines (the forming bar included), parsed and built by duo/sys_data.py
into a window of history_days rows, then the same targets(). No network.

The comparison is by exact column name, so each sleeve goes to the Kraken book the research's name maps to, not by coin:
a window can't see a break older than itself, and before duo/research_breaks.json the runner called Terra 2.0 plain LUNA
where the research calls it LUNA~2 and the cost model sends the two to different books (review finding
live-panel-column-names-diverge-luna, which the earlier coin-level comparison couldn't catch).

RESEARCH_ROOT points at a scratch folder that links to the development panel, so research's load_panel writes its read
log there and nothing under research/ changes.
"""
import bisect
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np

from duo import config as config_module
from duo import sys_data, sys_research

PROJECT = Path(__file__).resolve().parent.parent
DEV_PANEL = PROJECT / "data" / "research" / "dev" / "spot_1d.npz"
DAY = 86400
VARIANTS = ("F1__ENS__BTC_ETH__vol40", "F1__sma_50__TOP10__vol40", "F2__TOP10__L28__k3__invvol__nofilter")
DAYS = 120


def by_column(assets, weights, bands=None) -> dict:
    out = {}
    for c, name in enumerate(assets):
        if weights[c]:
            out[name] = (float(weights[c]), None if bands is None else float(bands[c]))
    return out


def as_klines(raw, column: int, rows: np.ndarray) -> list:
    out = []
    for k in rows:
        ts = int(raw.ts[k])
        values = [raw.open[k, column], raw.high[k, column], raw.low[k, column], raw.close[k, column],
                  raw.volume[k, column], raw.quote_volume[k, column]]
        out.append([ts * 1000, *map(repr, map(float, values[:5])), ts * 1000 + DAY * 1000 - 1, repr(float(values[5])),
                    1, "0", "0", "0"])
    return out


@unittest.skipUnless(DEV_PANEL.exists(), "needs the research's development panel")
class Parity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="duo-parity-"))
        (cls.root / "data" / "research" / "dev").mkdir(parents=True)
        os.symlink(DEV_PANEL, cls.root / "data" / "research" / "dev" / "spot_1d.npz")
        cls.old_root = os.environ.get("RESEARCH_ROOT")
        os.environ["RESEARCH_ROOT"] = str(cls.root)
        from research.harness import clean, data
        cls.raw = data.load_panel("spot_1d", "dev")  # Stored rows, before cleaning: what Binance serves.
        cls.full = clean.spot_panel("dev")           # The research's own panel.
        cls.history = config_module.load().systematic.history_days

    @classmethod
    def tearDownClass(cls):
        if cls.old_root is None:
            os.environ.pop("RESEARCH_ROOT", None)
        else:
            os.environ["RESEARCH_ROOT"] = cls.old_root
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_the_breaks_table_is_the_researchs_own(self):
        cuts = {}
        for split in self.full.meta["splits"]:
            cuts.setdefault(split["asset"], []).append(split["first_bar_ts"])
        self.assertEqual(sys_data.known_cuts(), {a: sorted(v) for a, v in cuts.items()})

    def test_targets_equal_the_research_for_every_one_of_the_last_120_development_days(self):
        raw, full = self.raw, self.full
        strategies = {v: sys_research.resolve(v) for v in VARIANTS}
        reference = {v: sys_research.targets(s, full) for v, s in strategies.items()}
        self.assertTrue(np.array_equal(raw.ts, full.ts))

        # Every asset's whole kline history, through the runner's own parser once. Each day then takes the rows
        # Binance's limit would return: the last history_days closed bars up to that day.
        parsed, stamps = {}, {}
        for c, asset in enumerate(raw.assets):
            rows = np.flatnonzero(~np.isnan(raw.close[:, c]))
            parsed[asset] = sys_data.parse_klines(as_klines(raw, c, rows), now=float(raw.ts[-1]) + 10 * DAY)
            stamps[asset] = [r[0] for r in parsed[asset]]
            self.assertEqual(len(parsed[asset]), rows.size, f"{asset}: the parser dropped a closed bar")

        checked = {v: 0 for v in VARIANTS}
        held_days = {v: 0 for v in VARIANTS}
        for t in range(raw.ts.size - DAYS, raw.ts.size):
            day = int(raw.ts[t])
            now = day + DAY + 120  # 00:02 UTC after the bar closed.
            first = day - (self.history - 1) * DAY
            trading = [a for c, a in enumerate(raw.assets) if not np.isnan(raw.close[t, c])]

            # BTC and ETH: the full path, Binance's JSON with the forming bar on the end, which must be dropped.
            fixed = {}
            for asset in ("BTC", "ETH"):
                c = raw.assets.index(asset)
                rows = np.flatnonzero((raw.ts >= first) & (raw.ts <= day) & ~np.isnan(raw.close[:, c]))
                served = as_klines(raw, c, rows)
                forming = list(served[-1])
                forming[0], forming[6] = (day + DAY) * 1000, (day + 2 * DAY) * 1000 - 1
                forming[4] = repr(float(raw.close[t, c]) * 50)  # Absurd, so any use of it shows.
                fixed[asset] = sys_data.parse_klines(served + [forming], now)
                self.assertEqual(fixed[asset][-1][0], day)
            panel = sys_data.build_panel(fixed, day, self.history)
            self._same("F1__ENS__BTC_ETH__vol40", strategies, reference, panel, t, checked, held_days)

            # TOP universes: every pair trading that day, as exchangeInfo would list.
            bars = {}
            for asset in trading:
                lo = bisect.bisect_left(stamps[asset], first)
                hi = bisect.bisect_right(stamps[asset], day)
                bars[asset] = parsed[asset][lo:hi]
            panel = sys_data.build_panel(bars, day, self.history)
            self.assertEqual(int(panel.ts[-1]), day)
            # Every column trading that day has the research's own name, so it goes to the research's Kraken book.
            live = sorted(a for c, a in enumerate(panel.assets) if not np.isnan(panel.close[-1, c]))
            research = sorted(a for c, a in enumerate(full.assets) if not np.isnan(full.close[t, c]))
            self.assertEqual(live, research, f"column names on {day}")
            for variant in VARIANTS[1:]:
                self._same(variant, strategies, reference, panel, t, checked, held_days)

        self.assertEqual(checked, {v: DAYS for v in VARIANTS})
        for variant, days in held_days.items():
            self.assertGreater(days, 0, f"{variant} held nothing on any day, so the test would prove little")
        print(f"\nParity on {DAYS} development days, {self.raw.ts.size - DAYS} to {self.raw.ts.size - 1}: "
              + ", ".join(f"{v} held something on {held_days[v]} days" for v in VARIANTS))

    def _same(self, variant, strategies, reference, panel, t, checked, held_days):
        target, band = sys_research.targets(strategies[variant], panel)
        mine = by_column(panel.assets, target[-1], band[-1])
        theirs = by_column(self.full.assets, reference[variant][0][t], reference[variant][1][t])
        with self.subTest(variant=variant, day=int(self.full.ts[t])):
            self.assertEqual(set(mine), set(theirs))
            for column, (weight, band_value) in theirs.items():
                self.assertAlmostEqual(mine[column][0], weight, places=12)
                self.assertAlmostEqual(mine[column][1], band_value, places=12)
        checked[variant] += 1
        held_days[variant] += bool(theirs)


if __name__ == "__main__":
    unittest.main()
