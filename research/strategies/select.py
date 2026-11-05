"""Section 11's selection, by code, once per track, on development data. research/STRATEGY_SPEC.md's "Selection, by
code", "Baselines and fallbacks" and "F5" sections define it, with Amendments 2, 4 and 6.

For each family, in this order: (1) every base variant, kind="trial"; (2) each at costs x2, kind="sensitivity";
(3) for those with after-cost return above 0 at both and max drawdown at most 50%, every plus or minus 20% neighbour
from gate.neighbours, kind="trial" under its own id; (4) the family walk-forward over all base variants, paying its
first entry; (5) the null percentile, 1,000 seeds, for those still standing after tests 1 to 3; (6) gate.eligible and
the winner by gate.selection_score. Every base variant also gets the always-reported sensitivities, and the top 3 by
Sharpe in each family, every winner and both fallbacks get a break-even fee. Baselines run on each track from both
starts. F5 runs through the same six stages if two or more families have winners. The outcome goes to
research/artifacts/selection_<track>.json, byte for byte the same on a repeat.

Every run goes through registry.run_variant naming its panels, so a repeat is answered from the record and the driver
can be stopped and started again. Runs that don't depend on each other go to at most 6 spawned worker processes. The
parent process never loads a panel and never runs a strategy, because the registry refuses a run in a process whose
worker read a panel it didn't name while that run was going.

Ids: a base variant is F1__ret_60__BTC_ETH__vol40, a neighbour adds what changed (F1__ret_60__BTC_ETH__vol40__L=72),
costs x2 adds x2, a fee level adds its name (tier1_taker), a break-even step adds fee_bps=12.5, and track R adds __R at
the end. The registry keeps one returns file per id, kind and split, so each of these needs its own id. Recorded params
are the strategy's params plus track, fee_level and cost_scale (and fee_bps where set), so that an id has one set of
params and the record says what each run cost. The driver refuses to use an id the record holds with other params.

Where the rules leave a choice open, these are the readings taken:

- "Every variant gets the always-reported sensitivities" is read as every variant tried: every base variant, every
  neighbour, every F5 variant and its neighbours, and FALLBACK_DCA. The always-reported ones are section 5's: costs
  x2 and the track's fee levels. A neighbour's run under them is kind="sensitivity", under the neighbour's id with the
  setting added (F1__ret_60__BTC_ETH__vol40__L=72__x2). They're reported, and test 3 reads only the base cost level.
- A neighbour whose params equal another run's, such as F4's Z of -2.4 reached from both -2 and -3, is still run and
  logged under its own id, so both count as trials in DSR_raw. That can only make the correction stricter.
- The null runs aren't on the record: run_variant takes a Result, and a null gives 1,000 Sharpes. Its percentile, its
  seeds (0 to 999, np.random.default_rng(seed + i) inside baselines.py), a summary of the Sharpes and the sha256 of
  the Sharpes' float64 bytes in seed order go in the selection file, so it can be reproduced and checked exactly
  (Amendment 7). They're split into chunks of seeds across workers, and the chunks give the
  same runs as one call, because run i always uses seed i.
- The null runs for those still standing after tests 1 to 3, as the task says, whatever test 4 gives. FALLBACK_TREND
  gets its neighbours and its null whatever tests 1 and 2 give, so all its section 11 tests are reported.
- A variant that failed test 1 or 2 has no neighbours run, and one that failed test 3 has no null run. gate.eligible
  then fails test 3 on the empty list or test 5 on the missing percentile, and the file marks those tests "not run".
- F1's null gets the variant's own band, the T x A array from f1_trend.targets, and TOP10's mask. F2's null,
  random_picks, has no band, and takes the variant's own top-n mask. F3's gets TOP5's mask. F4's random_trades takes
  neither. Each null's panel is the one the variant ran on: spot_1d, perps with spot_1d, or F4's own bars.
- "Ties to the earlier id" is the id that sorts first, as walk_forward's ties go to the first name in sorted order.
- The top 3 by Sharpe are among each family's base variants, by after-cost Sharpe at base costs, ties to the earlier
  id, and a NaN Sharpe last.
- The break-even fee replaces only the fee (fee_bps), keeping spread, impact, FX and the perp's costs, as the engines'
  fee_bps does. It's found in steps of 0.5 bps: 0, then 64 bps doubling until the return isn't above 0 (at most
  4,096), then halving the bracket. The file gives the highest fee tried with a return above 0 and the lowest without
  one, 0.5 bps apart. A variant not above 0 at a fee of 0 has none, and one still above 0 at 4,096 bps says so. Each
  step is a run on the record, kind="sensitivity". F5's steps run its members at that fee too.
- Baselines run from both 2018-07-01 and F3's 2020-04-07, because F3 compares over its own window. FALLBACK_DCA is the
  DCA into BTC and ETH from 2018-07-01. Its section 11 tests aren't run: it has no family, walk-forward or null. Its
  costs x2, sensitivities and break-even are.
- F5's null is decided for each F5 variant from its own members. Where they're all F1 or F2, it's random_spells on
  its mixed weights with a band of 10.0, so a spell trades only to enter and leave. F5 has no band of its own, and
  this is the cheapest null, which makes test 5 the hardest to pass. Its mask is the union of its members' universes:
  a TOP member's mask, and every day for the assets a fixed-universe member held. Where one of its members is F3 or
  F4, its test 5 is the lowest of its own members' percentiles, never another winner's, and the file says so.
- The regime labels for the report are regime_labels on BTC's closes in spot_1d, and F4's halt bars are worked out
  from majors_15m, both in a worker, so the report needn't read a panel. A halt is 2 hours or more between the close
  of a 15-minute bar with data and the open of the next. A longer bar is flagged if its open comes from the first
  15-minute bar after such a halt.
"""
from __future__ import annotations

import hashlib
import json
import math
import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone

import numpy as np

from research.harness import baselines, gate, registry, walkforward
from research.harness.data import _root
from research.harness.engine import EVAL_START_TS
from research.harness.metrics import summarise
from research.harness.regimes import REGIMES, regime_labels
from research.harness.result import Result
from research.strategies import common, f1_trend, f2_xsmom, f3_carry, f4_meanrev, f5_portfolios
from research.strategies.common import DAY, F3_START_TS, TRACKS

FAMILIES = {"F1": f1_trend, "F2": f2_xsmom, "F3": f3_carry, "F4": f4_meanrev}
WEIGHT_BASED = ("F1", "F2")  # Families whose Results random_spells can re-place. F3 is carry, F4 is trades.
FALLBACK_TREND = "F1__ENS__BTC_ETH__vol40"
FALLBACK_DCA = "dca_BTC_ETH"
NULL_SEEDS = 1000
NULL_CHUNK = 125  # Seeds per worker task. Run i always uses seed i, so the split changes nothing.
F5_NULL_BAND = 10.0  # No change in a weight can be this big, so a spell only trades to enter and leave.
MAX_WORKERS = 6  # BRIEF.md: at most 6 cores.
TOP_BY_SHARPE = 3
FEE_STEP_BPS = 0.5
FEE_FIRST_BPS = 64.0
FEE_CAP_BPS = 4096.0
HALT_SECONDS = 2 * 3600
BAR_SECONDS = {"15m": 900, "1h": 3600, "4h": 14400}
BASELINES = {
    "buy_and_hold_BTC": {"baseline": "buy_and_hold", "assets": ["BTC"]},
    "equal_weight_BTC_ETH": {"baseline": "equal_weight", "assets": ["BTC", "ETH"]},
    "top10_monthly": {"baseline": "top_n_monthly", "n": 10},
    "dca_BTC": {"baseline": "dca", "assets": ["BTC"], "weeks": 52},
    "dca_BTC_ETH": {"baseline": "dca", "assets": ["BTC", "ETH"], "weeks": 52},
}
STARTS = {"2018-07-01": EVAL_START_TS, "2020-04-07": F3_START_TS}
BASE = {"kind": "trial", "suffix": [], "cost_scale": 1.0, "fee_level": None, "fee_bps": None}


# Settings: how a run differs from its base variant's, and what that adds to its id.

def _x2() -> dict:
    return {**BASE, "kind": "sensitivity", "suffix": ["x2"], "cost_scale": 2.0}


def _level(level: str) -> dict:
    return {**BASE, "kind": "sensitivity", "suffix": [level], "fee_level": level}


def _fee(bps: float) -> dict:
    return {**BASE, "kind": "sensitivity", "suffix": [f"fee_bps={_text(bps)}"], "fee_bps": bps}


def _text(value) -> str:
    return str(value) if isinstance(value, int) else format(float(value), ".10g")


def _recorded(params: dict, track: str, setting: dict) -> dict:
    out = {**params, "track": track, "fee_level": setting["fee_level"] or TRACKS[track]["fee_level"],
           "cost_scale": setting["cost_scale"]}
    if setting["fee_bps"] is not None:
        out["fee_bps"] = setting["fee_bps"]
    return out


def _date(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d")


# Work done in a worker process. Every function here takes and returns plain data, so it can cross a process.

def _cost_model(track: str):
    return common.load_cost_model(track)


def _variant_result(task: dict) -> Result:
    """One run of a family variant, or F5, on the record."""
    track, family, setting = task["track"], task["family"], task["setting"]
    level = setting["fee_level"] or TRACKS[track]["fee_level"]
    if family == "F5":
        return _f5_result(task, level)
    module = FAMILIES[family]
    data = common.load_data("dev")  # Before run_variant, so the read isn't inside fn.

    def fn():
        return module.run(task["params"], data, _cost_model(track), level, setting["cost_scale"], setting["fee_bps"])

    return registry.run_variant(family, task["id"], task["recorded"], fn, kind=setting["kind"], panels=module.PANELS)


def _f5_result(task: dict, level: str) -> Result:
    track, setting = task["track"], task["setting"]
    member_tasks = _member_tasks(task)
    members = [_variant_result(member) for member in member_tasks]
    panels = sorted({"spot_1d", *(p for m in member_tasks for p in FAMILIES[m["family"]].PANELS)})

    def fn():
        return f5_portfolios.mix(members, task["params"], start_ts=task["start_ts"], cost_model=_cost_model(track),
                                 fee_level=level, cost_scale=setting["cost_scale"], fee_bps=setting["fee_bps"],
                                 name=task["id"])

    return registry.run_variant("F5", task["id"], task["recorded"], fn, kind=setting["kind"], panels=tuple(panels))


def _member_tasks(task: dict) -> list[dict]:
    """F5's members at F5's own setting: its x2 run mixes their x2 runs, and so on. A neighbour's members are base."""
    setting = task["setting"] if task["setting"]["kind"] == "sensitivity" else BASE
    # A neighbour's sensitivity runs its members at that cost setting only: the neighbour's change is F5's own.
    setting = {**setting, "suffix": setting.get("cost_suffix", setting["suffix"])}
    members = [task["winners"][name] for name in task["params"]["members"]]
    return [_run_task(task["track"], m["family"], m["params"], m["parts"], setting) for m in members]


def _baseline_result(task: dict) -> Result:
    track, setting, spec = task["track"], task["setting"], task["spec"]
    level = setting["fee_level"] or TRACKS[track]["fee_level"]
    spot = common.load_data("dev")["spot"]

    def fn():
        options = {"start_ts": task["start_ts"], "cost_model": _cost_model(track), "fee_level": level,
                   "cost_scale": setting["cost_scale"], "fee_bps": setting["fee_bps"], "name": task["id"]}
        kind = spec["baseline"]
        if kind == "buy_and_hold":
            return baselines.buy_and_hold(spot, spec["assets"][0], **options)
        if kind == "equal_weight":
            return baselines.equal_weight(spot, spec["assets"], None, **options)
        if kind == "top_n_monthly":
            return baselines.top_n_monthly(spot, n=spec["n"], **options)
        if kind == "dca":
            return baselines.dca(spot, spec["assets"], weeks=spec["weeks"], **options)
        raise ValueError(f"unknown baseline {kind!r}")

    return registry.run_variant("BASE", task["id"], task["recorded"], fn, kind=task["kind"], panels=("spot_1d",))


def _slim(result: Result) -> Result:
    """The Result with only the columns it ever held, for walk_forward, which reads nothing else.

    walk_forward prices an asset only where a weight changes, and reads a missing one as 0, so this changes no number.
    """
    held = np.flatnonzero((result.weights != 0).any(axis=0))
    return Result(result.name, result.ts, result.returns, result.gross_returns, result.turnover, result.exposure,
                  result.costs, result.weights[:, held], [result.assets[a] for a in held],
                  {"carry": bool(result.info.get("carry"))})


def _null_chunk(task: dict) -> dict:
    """One chunk of a variant's null: how many runs were strictly below its Sharpe, and their Sharpes."""
    track, family, params = task["track"], task["family"], task["params"]
    result = _variant_result(task)  # On the record already, so this reads it back.
    data = common.load_data("dev")
    spot = data["spot"]
    options = {"seeds": task["seeds"], "seed": task["seed"], "cost_model": _cost_model(track),
               "fee_level": TRACKS[track]["fee_level"]}
    if family == "F1":
        _, band = f1_trend.targets(params, spot)
        out = baselines.random_spells(result, spot, universe_mask=f1_trend.universe_mask(params, data), band=band,
                                      **options)
    elif family == "F2":
        out = baselines.random_picks(result, spot, f2_xsmom.universe_mask(params, data), **options)
    elif family == "F3":
        out = baselines.random_carry_spells(result, spot, data["perps"],
                                            universe_mask=f3_carry.universe_mask(params, data), **options)
    elif family == "F4":
        out = baselines.random_trades(result, f4_meanrev.bar_panel(params, data["majors"]), **options)
    elif family == "F5":
        out = baselines.random_spells(result, spot, universe_mask=_f5_mask(task, data), band=F5_NULL_BAND, **options)
    else:
        raise ValueError(f"no null for {family}")
    return {"below": int(round(out["percentile"] * task["seeds"])), "sharpes": out["sharpes"].tolist()}


def _f5_mask(task: dict, data: dict):
    """The union of F5's members' universes on spot's rows, or None if none of them has one."""
    spot = data["spot"]
    masks, fixed = [], set()
    for member in _member_tasks({**task, "setting": BASE}):
        mask = FAMILIES[member["family"]].universe_mask(member["params"], data)
        if mask is None:
            held = _variant_result(member)
            fixed |= {asset for a, asset in enumerate(held.assets) if (held.weights[:, a] > 0).any()}
        else:
            masks.append(mask)
    if not masks:
        return None
    union = np.logical_or.reduce(masks)
    for asset in fixed:
        union[:, spot.assets.index(asset)] = True
    return union


def _break_even(task: dict) -> dict:
    """The fee in bps where after-cost total return stops being above 0, found to within FEE_STEP_BPS."""
    tried = {}

    def above_zero(steps: int) -> bool:
        if steps not in tried:
            bps = steps * FEE_STEP_BPS
            run = {**task["run"], "setting": _fee(bps)}
            run["id"] = common.variant_id(run["family_id"], [*run["parts"], *run["setting"]["suffix"]], run["track"])
            run["recorded"] = _recorded(run["params"], run["track"], run["setting"])
            result = _baseline_result(run) if run["op"] == "baseline" else _variant_result(run)
            tried[steps] = summarise(result)["total_return"]
        return tried[steps] > 0  # A NaN isn't above 0.

    task = {**task, "run": {**task["run"], "kind": "sensitivity"}}  # A baseline's fee steps are its sensitivities.
    out = {"low_bps": None, "high_bps": None}
    if not above_zero(0):
        out.update(high_bps=0.0, note="not above 0 even with no fee")
    else:
        low, high = 0, int(FEE_FIRST_BPS / FEE_STEP_BPS)
        while above_zero(high):
            low = high
            if high * FEE_STEP_BPS >= FEE_CAP_BPS:
                break
            high *= 2
        if low == high:
            out.update(low_bps=low * FEE_STEP_BPS, note="still above 0 at the cap")
        else:
            while high - low > 1:
                middle = (low + high) // 2
                low, high = (middle, high) if above_zero(middle) else (low, middle)
            out.update(low_bps=low * FEE_STEP_BPS, high_bps=high * FEE_STEP_BPS)
    out["tried"] = [[steps * FEE_STEP_BPS, tried[steps]] for steps in sorted(tried)]
    return out


def _context(task: dict) -> dict:
    """BTC's regime labels on spot_1d's days, and the bars F4 can enter at just after a halt."""
    data = common.load_data("dev")
    spot, majors = data["spot"], data["majors"]
    common.check_spacing(spot, DAY)
    labels = regime_labels(data["btc_close"], spot.ts)
    regimes = {"first_ts": int(spot.ts[0]), "days": int(spot.ts.size),
               **{name: "".join("1" if flag else "0" for flag in labels[name]) for name in REGIMES}}
    halts = {bar: {} for bar in BAR_SECONDS}
    for column, asset in enumerate(majors.assets):
        stamps = majors.ts[~np.isnan(majors.close[:, column])]
        after = np.flatnonzero(stamps[1:] - (stamps[:-1] + BAR_SECONDS["15m"]) >= HALT_SECONDS) + 1
        for bar, seconds in BAR_SECONDS.items():
            starts = stamps[after] // seconds * seconds
            # The bar's open comes from the halt's end only if no 15-minute bar with data opened earlier in it.
            flagged = starts[stamps[after - 1] < starts]
            halts[bar][asset] = sorted({int(stamp) for stamp in flagged})
    return {"regimes": regimes, "f4_halts": halts}


def _work(task: dict) -> dict:
    op = task["op"]
    if op == "run":
        result = _variant_result(task)
        return {"metrics": summarise(result), "slim": _slim(result) if task.get("slim") else None}
    if op == "baseline":
        return {"metrics": summarise(_baseline_result(task))}
    if op == "null":
        return _null_chunk(task)
    if op == "break_even":
        return _break_even(task)
    if op == "context":
        return _context(task)
    raise ValueError(f"unknown op {op!r}")


# The parent: tasks, the pool, and the stages.

def _run_task(track: str, family: str, params: dict, parts: list, setting: dict, *, slim=False, **extra) -> dict:
    vid = common.variant_id(family, [*parts, *setting["suffix"]], track)
    return {"op": "run", "track": track, "family": family, "family_id": family, "id": vid, "params": params,
            "parts": list(parts), "setting": setting, "recorded": _recorded(params, track, setting), "slim": slim,
            **extra}


def _baseline_task(track: str, name: str, start: str, setting: dict, kind="baseline") -> dict:
    spec = BASELINES[name]
    parts = [name, start]
    params = {**spec, "start": start}
    return {"op": "baseline", "track": track, "family": "BASE", "family_id": "BASE", "parts": parts, "spec": spec,
            "start_ts": STARTS[start], "params": params, "setting": setting, "kind": kind,
            "id": common.variant_id("BASE", [*parts, *setting["suffix"]], track),
            "recorded": _recorded(params, track, setting)}


class _Ids:
    """Every id the driver uses, with its params, checked against each other and the record."""

    def __init__(self):
        self.seen = {}
        path = _root() / "research" / "artifacts" / "variants.jsonl"
        if path.exists():
            for raw in path.read_bytes().splitlines():
                line = json.loads(raw)
                self.claim(line["family"], line["variant_id"], line["params"])

    def claim(self, family: str, vid: str, params: dict) -> None:
        text = json.dumps(params, sort_keys=True)
        known = self.seen.setdefault((family, vid), text)
        if known != text:
            raise ValueError(f"{family}/{vid} is already on the record with other params: {known} against {text}")


def _pool(workers: int):
    if workers <= 1:
        return None
    return ProcessPoolExecutor(max_workers=min(workers, MAX_WORKERS), mp_context=multiprocessing.get_context("spawn"))


def _map(pool, ids: _Ids, label: str, tasks: list[dict], say) -> list[dict]:
    """Runs the tasks, in a pool if there is one, and gives back their outputs in the same order."""
    for task in tasks:
        if "recorded" in task:
            ids.claim(task["family"], task["id"], task["recorded"])
    started, said = time.time(), time.time()
    out = [None] * len(tasks)
    if not tasks:
        return out
    if pool is None:
        done = ((index, _work(task)) for index, task in enumerate(tasks))
    else:
        futures = {pool.submit(_work, task): index for index, task in enumerate(tasks)}
        done = ((futures[future], future.result()) for future in as_completed(futures))
    for count, (index, value) in enumerate(done, start=1):
        out[index] = value
        if count == len(tasks) or time.time() - said > 10:
            said = time.time()
            say(f"  {label}: {count}/{len(tasks)} in {time.time() - started:.0f} s")
    return out


def _grids(grids) -> dict:
    """Each family's base variants, in FAMILIES order. A test can pass smaller grids, for some families only."""
    if grids is None:
        return {family: module.base_variants() for family, module in FAMILIES.items()}
    unknown = set(grids) - set(FAMILIES)
    if unknown:
        raise ValueError(f"unknown families {sorted(unknown)}")
    return {family: list(grids[family]) for family in FAMILIES if family in grids}


def _entries(track: str, family: str, grid: list[dict]) -> list[dict]:
    module = FAMILIES[family]
    out = []
    for params in grid:
        parts = module.parts(params)
        out.append({"family": family, "params": params, "parts": parts, "numeric": params,
                    "id": common.variant_id(family, parts, track), "base_id": common.variant_id(family, parts, "K")})
    if len({entry["id"] for entry in out}) != len(out):
        raise ValueError(f"{family}'s grid gives one id to two variants")
    return out


def _passes(tests: dict, *names) -> bool:
    return all(tests[name]["passes"] for name in names)


def _sharpe_order(variants: dict) -> list[str]:
    def key(vid):
        sharpe = variants[vid]["metrics"]["sharpe"]
        return (math.isnan(sharpe), 0.0 if math.isnan(sharpe) else -sharpe, vid)
    return sorted(variants, key=key)


def _families(track: str, entries: dict, pool, ids: _Ids, seeds: int, say, *, forced=(), extra=None,
              member_nulls=None) -> dict:
    """Section 11's six stages for these families. entries maps a family to its base variants.

    forced lists ids whose neighbours and null run whatever tests 1 to 3 give. extra adds keys to every run task (F5's
    members and start). member_nulls maps an F5 variant's id to the lowest of its own members' percentiles, where its
    own null can't run.
    """
    extra = extra or {}
    fee_level = TRACKS[track]["fee_level"]
    flat = [entry for family in entries for entry in entries[family]]
    task = lambda entry, setting, params=None, **more: _run_task(
        track, entry["family"], entry["params"] if params is None else params, entry["parts"], setting,
        **extra.get(entry["family"], {}), **more)

    say(f"[{track}] stage 1, base variants: {len(flat)}")
    base = _map(pool, ids, "base", [task(entry, BASE, slim=True) for entry in flat], say)
    say(f"[{track}] stage 2, costs x2: {len(flat)}")
    doubled = _map(pool, ids, "x2", [task(entry, _x2()) for entry in flat], say)
    levels = TRACKS[track]["sensitivities"]
    say(f"[{track}] always-reported sensitivities {levels}: {len(flat) * len(levels)}")
    reported = _map(pool, ids, "sensitivities", [task(entry, _level(level)) for entry in flat for level in levels], say)

    variants = {}
    for number, (entry, run, x2) in enumerate(zip(flat, base, doubled)):
        early = gate.eligible({"base": run["metrics"], "x2_costs": x2["metrics"]})
        variants[entry["id"]] = {
            "family": entry["family"], "params": entry["params"], "metrics": run["metrics"], "x2": x2["metrics"],
            "sensitivities": {level: reported[number * len(levels) + k]["metrics"] for k, level in enumerate(levels)},
            "passes_1_2": _passes(early, "test1", "test2"), "neighbours": None, "null": None,
        }

    # Stage 3: neighbours of those that passed tests 1 and 2, and of the forced, each with its sensitivities.
    plans = []
    costs = [_x2(), *(_level(level) for level in levels)]
    for entry in flat:
        if variants[entry["id"]]["passes_1_2"] or entry["id"] in forced:
            spec = f5_portfolios.NUMERIC if entry["family"] == "F5" else FAMILIES[entry["family"]].NUMERIC
            numeric = f5_portfolios.numeric(entry["params"]) if entry["family"] == "F5" else entry["params"]
            for name, direction, moved in gate.neighbours(numeric, spec):
                params = {**entry["params"], name: moved[name]}
                change = f"{name}={_text(moved[name])}"
                runs = [task(entry, {**BASE, "suffix": [change]}, params)]
                runs += [task(entry, {**cost, "suffix": [change, *cost["suffix"]], "cost_suffix": cost["suffix"]},
                              params) for cost in costs]
                plans.append((entry, name, direction, runs))
            variants[entry["id"]]["neighbours"] = []
    say(f"[{track}] stage 3, neighbours: {len(plans)}, each with {len(costs)} sensitivities")
    ran = iter(_map(pool, ids, "neighbours", [run for plan in plans for run in plan[3]], say))
    for entry, name, direction, runs in plans:
        outcome = [next(ran) for _ in runs]
        variants[entry["id"]]["neighbours"].append(
            {"id": runs[0]["id"], "param": name, "direction": direction, "value": runs[0]["params"][name],
             "metrics": outcome[0]["metrics"],
             "sensitivities": {cost["suffix"][0]: run["metrics"] for cost, run in zip(costs, outcome[1:])}})

    # Stage 4: each family's walk-forward over all its base variants, paying its first entry.
    say(f"[{track}] stage 4, walk-forward")
    out = {}
    cost_model = _cost_model(track)
    slim = {entry["id"]: run["slim"] for entry, run in zip(flat, base)}
    for family in entries:
        results = {entry["id"]: slim[entry["id"]] for entry in entries[family]}
        wf = walkforward.walk_forward(results, cost_model=cost_model, fee_level=fee_level, charge_first_entry=True)
        out[family] = {"walk_forward": {
            "sharpe": wf["sharpe"], "gap": wf["gap"], "best_in_sample": list(wf["best_in_sample"]),
            "picks": [[int(stamp), name] for stamp, name in wf["picks"]], "switch_costs": wf["switch_costs"].tolist(),
            "first_ts": int(wf["ts"][0]), "fee_level": fee_level, "charge_first_entry": True}}
        out[family]["first_ts"] = int(next(iter(results.values())).ts[0])

    # Stage 5: the null for those standing after tests 1 to 3, and the forced.
    standing = []
    for entry in flat:
        info = variants[entry["id"]]
        tests = gate.eligible(_report(info, None, None, entry))
        if (_passes(tests, "test1", "test2", "test3") or entry["id"] in forced) and info["neighbours"] is not None:
            if entry["id"] in (member_nulls or {}):
                info["null"] = member_nulls[entry["id"]]
            else:
                standing.append(entry)
    chunks = [(entry, start) for entry in standing for start in range(0, seeds, NULL_CHUNK)]
    say(f"[{track}] stage 5, nulls: {len(standing)} variants x {seeds} seeds")
    pieces = _map(pool, ids, "nulls", [
        {**task(entry, BASE), "op": "null", "seed": start, "seeds": min(NULL_CHUNK, seeds - start)}
        for entry, start in chunks], say)
    for entry in standing:
        mine = [piece for (owner, _), piece in zip(chunks, pieces) if owner is entry]
        sharpes = np.array([value for piece in mine for value in piece["sharpes"]], dtype=np.float64)
        family = entry["family"]
        variants[entry["id"]]["null"] = {
            "percentile": sum(piece["below"] for piece in mine) / seeds, "seeds": seeds, "first_seed": 0,
            "median_sharpe": float(np.nanmedian(sharpes)) if np.isfinite(sharpes).any() else None,
            "p95_sharpe": float(np.nanquantile(sharpes, 0.95)) if np.isfinite(sharpes).any() else None,
            "nan_runs": int(np.isnan(sharpes).sum()),
            # The Sharpes in seed order as float64 bytes, so a rerun of any null can be checked against this one.
            "sharpes_sha256": hashlib.sha256(sharpes.tobytes()).hexdigest(),
            "how": {"F1": "random_spells, own band, TOP mask", "F2": "random_picks, own top-n mask",
                    "F3": "random_carry_spells, TOP mask", "F4": "random_trades",
                    "F5": "random_spells on the mixed weights, band 10.0, members' universes"}[family],
        }

    # Stage 6: the gate's five tests, and each family's winner.
    for family in entries:
        mine = {entry["id"]: variants[entry["id"]] for entry in entries[family]}
        wf_sharpe = out[family]["walk_forward"]["sharpe"]
        eligible = []
        for entry in entries[family]:
            info = mine[entry["id"]]
            info["tests"] = gate.eligible(_report(info, wf_sharpe, info["null"], entry))
            info["not_run"] = [name for name, missing in (("test3", info["neighbours"] is None),
                                                          ("test5", info["null"] is None)) if missing]
            info["score"] = None
            if info["tests"]["eligible"]:
                info["score"] = gate.selection_score(info["metrics"]["sharpe"],
                                                     [n["metrics"]["sharpe"] for n in info["neighbours"]])
                eligible.append(entry["id"])
        winner = min(eligible, key=lambda vid: (-mine[vid]["score"], vid)) if eligible else None
        out[family].update({
            "variants": mine, "eligible": eligible, "winner": winner,
            "top_by_sharpe": _sharpe_order(mine)[:TOP_BY_SHARPE],
            "counts": {"variants": len(mine), **{f"test{k}": sum(v["tests"][f"test{k}"]["passes"]
                                                                  for v in mine.values()) for k in range(1, 6)},
                       "neighbour_runs": sum(len(v["neighbours"] or ()) for v in mine.values()),
                       "null_runs": sum(v["null"] is not None for v in mine.values()),
                       "eligible": len(eligible)},
        })
        say(f"[{track}] {family}: {len(eligible)} eligible of {len(mine)}, winner {winner}")
    return out


def _report(info: dict, wf_sharpe, null, entry) -> dict:
    report = {"base": info["metrics"], "x2_costs": info["x2"], "neighbours": info["neighbours"] or [],
              "walk_forward_sharpe": wf_sharpe, "null_percentile": None if null is None else null["percentile"]}
    report["neighbours"] = [n["metrics"] for n in report["neighbours"]]
    return report


def _f5(track: str, winners: dict, family_out: dict, entries: dict, pool, ids: _Ids, seeds: int, say) -> dict:
    """F5 through the same stages, if two or more families have winners."""
    if len(winners) < 2:
        say(f"[{track}] F5 skipped: {len(winners)} family winner(s)")
        return {"skipped": f"{len(winners)} family winner(s), and F5 needs at least two"}, [], {}
    members = {}
    for family, vid in winners.items():
        entry = next(entry for entry in entries[family] if entry["id"] == vid)
        members[entry["base_id"]] = {"family": family, "params": entry["params"], "parts": entry["parts"]}
    start_ts = max(family_out[family]["first_ts"] for family in winners)
    grid = []
    for params in f5_portfolios.variants(members):
        parts = f5_portfolios.parts(params)
        recorded = {**params, "start": _date(start_ts)}
        grid.append({"family": "F5", "params": recorded, "parts": parts, "id": common.variant_id("F5", parts, track),
                     "base_id": common.variant_id("F5", parts, "K")})
    # Test 5 is decided for each variant from its own members: random_spells where they're all weight-based, and
    # otherwise the lowest of its own members' percentiles.
    member_nulls = {}
    for entry in grid:
        own = {vid: members[vid]["family"] for vid in entry["params"]["members"]}
        if all(family in WEIGHT_BASED for family in own.values()):
            continue
        percentiles = {vid: family_out[own[vid]]["variants"][_track_id(vid, track)]["null"]["percentile"]
                       for vid in own}
        member_nulls[entry["id"]] = {
            "percentile": min(percentiles.values()), "seeds": None, "members": percentiles,
            "how": "the lowest of its own members' percentiles, because one of them is F3 or F4 and random_spells "
                   "can't re-place its positions"}
    extra = {"F5": {"winners": members, "start_ts": start_ts}}
    out = _families(track, {"F5": grid}, pool, ids, seeds, say, extra=extra, member_nulls=member_nulls)["F5"]
    out.update({"members": sorted(members), "start_ts": start_ts,
                "members_weight_based": all(member["family"] in WEIGHT_BASED for member in members.values()),
                "test5_from_members": sorted(member_nulls)})
    return out, grid, extra["F5"]


def _track_id(base_id: str, track: str) -> str:
    """A base id (as F5's members are named) as this track names it."""
    return base_id + ("__R" if track == "R" else "")


def _break_even_tasks(track: str, families: dict, entries: dict, extra: dict) -> list[dict]:
    """The top 3 by Sharpe in each family, F5 included, every winner, and both fallbacks."""
    targets = []
    for family, out in families.items():
        for vid in out["top_by_sharpe"] + ([out["winner"]] if out["winner"] else []):
            targets.append((family, vid))
    targets.append(("F1", common.variant_id("F1", FALLBACK_TREND.split("__")[1:], track)))
    tasks, seen = [], set()
    for family, vid in targets:
        if vid in seen:
            continue
        seen.add(vid)
        entry = next(entry for entry in entries[family] if entry["id"] == vid)
        run = _run_task(track, family, entry["params"], entry["parts"], BASE, **extra.get(family, {}))
        tasks.append({"op": "break_even", "id": vid, "run": run})
    dca = _baseline_task(track, FALLBACK_DCA, "2018-07-01", BASE)
    tasks.append({"op": "break_even", "id": dca["id"], "run": dca})
    return tasks


def select_track(track: str, *, grids=None, seeds: int = NULL_SEEDS, workers: int = MAX_WORKERS, say=print) -> dict:
    """Runs section 11 on one track and writes research/artifacts/selection_<track>.json. Returns what it wrote."""
    if track not in TRACKS:
        raise ValueError(f"track must be one of {sorted(TRACKS)}, not {track!r}")
    grids = _grids(grids)
    if "F1" not in grids:
        raise ValueError("F1 must be run: FALLBACK_TREND is always reported")
    entries = {family: _entries(track, family, grid) for family, grid in grids.items()}
    fallback = common.variant_id("F1", FALLBACK_TREND.split("__")[1:], track)
    if fallback not in {entry["id"] for entry in entries["F1"]}:
        raise ValueError(f"F1's grid must hold FALLBACK_TREND, {fallback}")
    ids = _Ids()
    pool = _pool(workers)
    started = time.time()
    try:
        families = _families(track, entries, pool, ids, seeds, say, forced=(fallback,))
        winners = {family: out["winner"] for family, out in families.items() if out["winner"]}
        f5_out, entries["F5"], f5_extra = _f5(track, winners, families, entries, pool, ids, seeds, say)
        ranked = {**families, **({} if "skipped" in f5_out else {"F5": f5_out})}

        say(f"[{track}] baselines")
        base_tasks = [_baseline_task(track, name, start, BASE) for name in BASELINES for start in STARTS]
        dca_tasks = [_baseline_task(track, FALLBACK_DCA, "2018-07-01", setting, kind="sensitivity")
                     for setting in (_x2(), *(_level(level) for level in TRACKS[track]["sensitivities"]))]
        ran = _map(pool, ids, "baselines", base_tasks + dca_tasks, say)
        baseline_out = {task["id"]: {"params": task["params"], "kind": task["kind"], "metrics": run["metrics"]}
                        for task, run in zip(base_tasks + dca_tasks, ran)}

        say(f"[{track}] break-even fees")
        be_tasks = _break_even_tasks(track, ranked, entries, {"F5": f5_extra})
        be = {task["id"]: out for task, out in zip(be_tasks, _map(pool, ids, "break-even", be_tasks, say))}
        context = _map(pool, ids, "context", [{"op": "context"}], say)[0]
    finally:
        if pool is not None:
            pool.shutdown()
    if f5_out.get("winner"):
        winners["F5"] = f5_out["winner"]
    selection = {
        "track": track, "fee_level": TRACKS[track]["fee_level"], "sensitivities": list(TRACKS[track]["sensitivities"]),
        "null_seeds": seeds, "families": families, "winners": winners, "F5": f5_out,
        "fallbacks": {"FALLBACK_TREND": fallback,
                      "FALLBACK_DCA": common.variant_id("BASE", [FALLBACK_DCA, "2018-07-01"], track)},
        "baselines": baseline_out, "break_even": be, **context,
    }
    path = _root() / "research" / "artifacts" / f"selection_{track}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(_plain(selection), sort_keys=True, indent=1, allow_nan=False) + "\n"
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text)
    temp.replace(path)
    say(f"[{track}] wrote {path} after {time.time() - started:.0f} s")
    return json.loads(text)


def _plain(value):
    """JSON-safe: numpy to Python, tuples to lists, NaN and infinity to None."""
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, np.ndarray):
        return _plain(value.tolist())
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value
