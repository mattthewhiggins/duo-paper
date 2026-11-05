"""The command line for section 11 and the report.

    python -m research.strategies.run --track K     # every stage of section 11 on track K's costs
    python -m research.strategies.run --track R     # the same on track R's
    python -m research.strategies.run --report      # RESULTS.md and variants_table.csv from the record
    python -m research.strategies.run --track K --dry-run

A track's stages run in order, with progress printed. Stopping and starting again is safe: the registry answers every
run it already holds from its cache, so a restart only does what's left. Runs that don't depend on each other go to at
most 6 worker processes.

--dry-run prints how many runs each stage makes, and runs nothing. Stages 3 and 5 and the break-even fees depend on
results, so for them it prints the most there can be.
"""
from __future__ import annotations

import argparse
import sys

from research.harness import gate
from research.strategies import f5_portfolios, report, select
from research.strategies.common import TRACKS


def dry_run(track: str, grids=None, seeds: int = select.NULL_SEEDS) -> dict:
    """Runs per stage, the uncertain ones as maxima. Reads no data and runs nothing."""
    grids = select._grids(grids)
    base = sum(len(grid) for grid in grids.values())
    neighbours = sum(len(gate.neighbours(params, select.FAMILIES[family].NUMERIC))
                     for family, grid in grids.items() for params in grid)
    levels = len(TRACKS[track]["sensitivities"])
    # A fee of 0, the doublings from 64 bps to the cap, then halving the last bracket down to 0.5 bps.
    doublings = int(select.FEE_CAP_BPS / select.FEE_FIRST_BPS).bit_length()
    halvings = int(select.FEE_CAP_BPS / 2 / select.FEE_STEP_BPS).bit_length() - 1
    steps = 1 + doublings + halvings
    targets = len(grids) * (select.TOP_BY_SHARPE + 1) + 1 + 1 + select.TOP_BY_SHARPE + 1  # Fallbacks, F5's top 3, winner.
    f5 = f5_portfolios.MAX_VARIANTS
    f5_neighbours = 3  # invvol's window both ways, and the rebalance interval's one step up.
    return {
        "1 base variants (trial)": base,
        "2 costs x2 (sensitivity)": base,
        "always-reported sensitivities": base * levels,
        "3 neighbours (trial), at most": neighbours,
        "3 neighbours' costs x2 and fee levels (sensitivity), at most": neighbours * (1 + levels),
        "5 nulls, at most (variants x seeds, not on the record)": f"{base} x {seeds}",
        "baselines and FALLBACK_DCA's sensitivities": len(select.BASELINES) * len(select.STARTS) + 1 + levels,
        "F5, at most (variants and neighbours, each with x2 and sensitivities)": f5 * (1 + f5_neighbours) * (2 + levels),
        "break-even steps, at most": f"{targets} x {steps}",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m research.strategies.run", description=__doc__.split("\n\n")[0])
    parser.add_argument("--track", choices=sorted(TRACKS), help="run section 11 on this track's costs")
    parser.add_argument("--report", action="store_true", help="write RESULTS.md and variants_table.csv")
    parser.add_argument("--dry-run", action="store_true", help="print the runs per stage and run nothing")
    parser.add_argument("--workers", type=int, default=select.MAX_WORKERS,
                        help=f"worker processes, at most {select.MAX_WORKERS}")
    args = parser.parse_args(argv)
    if bool(args.track) == args.report:
        parser.error("give one of --track K, --track R or --report")
    if args.workers < 1 or args.workers > select.MAX_WORKERS:
        parser.error(f"--workers must be 1 to {select.MAX_WORKERS}")
    if args.report:
        if args.dry_run:
            parser.error("--dry-run is for a track")
        report.build()
        return 0
    if args.dry_run:
        for stage, count in dry_run(args.track).items():
            print(f"{stage}: {count}")
        return 0
    select.select_track(args.track, workers=args.workers, say=lambda text: print(text, flush=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
