"""Section 11's eligibility tests and section 12's validation gate, over figures the backtests have already produced.

Nothing here reads data or runs a strategy. Every test fails closed: a missing value, None, NaN, an infinity or a
flag where a number belongs fails whichever way its threshold points, and an empty list never counts as all passed.
Likewise a number where a flag belongs, or a lone value where a list belongs.

Where the rules leave a choice open, these are the readings taken, each the one that's harder to pass:

- Test 2 reads base["max_drawdown"]. The specification's list of report fields leaves the drawdown out, and
  summarise provides it. A drawdown below 0 fails, because summarise gives a positive fraction, so -0.6 is a sign
  mistake for a 60% loss.
- null_percentile is a share from 0 to 1, as baselines returns it, so the threshold is 0.95. A figure above 1 fails,
  or a percentile of 50 given as a percentage would clear 0.95.
- G5's "section 11 test" is the whole of eligible, not only its plus or minus 20% test. The two differ only for a
  variant that reaches the gate without being eligible, such as a fallback evaluated for reporting.
- G5's holdout half fails on an empty list even with no numeric parameters, as the specification says, so such a
  variant can be eligible but can't pass the gate.
- G5's holdout half also fails on a list of any length but the number of neighbours test 3 read. Section 12 says "every
  neighbour", and a neighbour left out of the list has shown no more than one given as None. A longer list means the two
  periods didn't run the same neighbours, so a good return in it can't be tied to any of them.
- A numeric parameter with no entry in spec raises, because skipping it would leave it untested. So does a number
  inside a list, tuple or dict, such as a crossover written (20, 100): each needs its own parameter. Amendment 2 leaves
  categorical choices out, and a number can't say it's categorical, so a bar size has to be text, such as "15m".
- A parameter marked "derived" still gets its own plus and minus 20%, because amendment 2 covers every numeric
  parameter. "derived" is a function of the new params, and also recomputes it when another parameter moves.
- A dsr_eff that is NaN or missing gets the luck note, because it hasn't ruled luck out.
- G1 reads the return as section 7 marks it, at the close, so a position still open at the end has paid half its round
  trip. That stays the test. A pass that wouldn't survive paying the other half gets "exit_note" beside it, read from
  holdout["open_exit_cost"] if it's there, and like the luck note it never changes passes.
"""
from __future__ import annotations

import math
import numbers

import numpy as np

STEP = 0.20  # Section 11: a change of plus or minus 20%.
NEIGHBOUR_SHARPE_SHARE = 0.70  # Test 3: at 70% or more of the base.
MAX_DRAWDOWN = 0.50  # Test 2 and G3: 50% or less.
NULL_SHARE = 0.95  # Test 5: the 95th percentile or better.
HOLDOUT_SHARPE_SHARE = 0.50  # G2: at or above 0.5 x development.
MIN_REGIMES_WORKING = 2  # G6: at least 2 of 4.
REGIMES = ("trending", "ranging", "high_vol", "low_vol")
LUCK_DSR = 0.95
LUCK_NOTE = "passes the gate, but luck can't be ruled out"
EXIT_NOTE = "G1 passes only because the exit of a position still open at the end isn't charged"


def _is_number(value) -> bool:
    # A bool is an int in Python, but True isn't a return of 1 or a parameter to vary. numbers.Real, not (int, float),
    # because np.int64 is neither, and a parameter held in one would escape its neighbours.
    return isinstance(value, numbers.Real) and not isinstance(value, bool)


def _finite(value) -> float:
    """value as a float, or NaN if it isn't a finite number. Every comparison below is false on NaN."""
    return float(value) if _is_number(value) and math.isfinite(value) else math.nan


def _get(block, key) -> float:
    """One figure from a dict, NaN if it isn't there."""
    return _finite(block.get(key)) if isinstance(block, dict) else math.nan


def _as_list(value) -> list:
    """A list field as a list. Anything else is empty, so a lone number fails the way an empty list does."""
    if isinstance(value, np.ndarray):
        value = value.tolist()
    return list(value) if isinstance(value, (list, tuple)) else []


def _is_flag(value, expected: bool) -> bool:
    # Only a real flag counts, or None would read as not failing badly and "False" as working. by_regime compares
    # numpy figures, so its flags may be np.bool_.
    return isinstance(value, (bool, np.bool_)) and bool(value) is expected


def _lowest(values: list[float]) -> float:
    # np.min passes a NaN on, where min() would give an answer that depends on where the NaN sits.
    return float(np.min(values)) if values else math.nan


def _entry(value, threshold, passes) -> dict:
    return {"value": value, "threshold": threshold, "passes": bool(passes)}


def _drawdown_ok(drawdown: float) -> bool:
    return 0 <= drawdown <= MAX_DRAWDOWN


def _plain(value):
    # The registry writes params as JSON, which takes no numpy integer, even one a neighbour only carries over.
    return value.item() if isinstance(value, np.generic) else value


def _holds_numbers(value) -> bool:
    items = list(value.values()) if isinstance(value, dict) else _as_list(value)
    return any(_is_number(item) or _holds_numbers(item) for item in items)


def _whole(name: str, value) -> int:
    # Rounding a fraction would quietly move the base variant too, and "never below 1" can't hold for a negative.
    if value < 1 or value != int(value):
        raise ValueError(f"{name} is marked as an integer, but its value is {value}")
    return int(value)


def _moved_whole(value: int, sign: int) -> int | None:
    """The 20% move, rounded. If rounding undoes it, the next whole number that way. None if that's below 1."""
    moved = round(value * (1 + sign * STEP))
    if moved == value:
        moved = value + sign
    return moved if moved >= 1 else None


def _with_derived(new: dict, spec: dict, moved: str) -> dict:
    """new, with each derived parameter recomputed from it, apart from the one that was just moved."""
    for name, entry in spec.items():
        rule = entry.get("derived")
        if rule and name != moved and name in new:
            if not callable(rule):
                raise ValueError(f"{name}'s derived must be a function of the new params, got {rule!r}")
            value = rule(new)
            new[name] = max(1, round(value)) if entry["integer"] else value
    return new


def neighbours(params: dict, spec: dict) -> list[tuple[str, str, dict]]:
    """Every single-parameter change of plus or minus 20%, as (param_name, direction, new_params).

    spec maps each numeric parameter to {"integer": bool, "derived": optional}. See the top of this file for derived.
    direction is "-" for value x 0.8 and "+" for value x 1.2, so -2.0's "+" neighbour is -2.4. Integers round to the
    nearest whole number, and if that equals the original they move to the next whole number that way, never below 1.
    Non-numeric parameters and a value of 0 aren't varied, because 20% of 0 is 0. Duplicates appear once.
    """
    out = []
    params = {name: _plain(value) for name, value in params.items()}
    for name, value in params.items():
        if _holds_numbers(value):
            raise ValueError(f"{name} holds numbers inside it, and each needs its own parameter to be varied")
        if not _is_number(value) or value == 0:
            continue
        if not math.isfinite(value):
            raise ValueError(f"{name} has no finite value to vary: {value}")
        if "integer" not in spec.get(name, {}):
            raise ValueError(f"numeric parameter {name} has no entry in spec")
        integer = spec[name]["integer"]
        base = _whole(name, value) if integer else float(value)
        for direction, sign in (("-", -1), ("+", 1)):
            moved = _moved_whole(base, sign) if integer else base * (1 + sign * STEP)
            if moved is None:
                continue
            new = _with_derived({**params, name: moved}, spec, name)
            if all(new != other for _, _, other in out):
                out.append((name, direction, new))
    return out


def eligible(report: dict) -> dict:
    """Section 11's five tests, on development figures.

    report holds base and x2_costs (metrics dicts), neighbours (a list of metrics dicts, one for each run of a
    variant from neighbours()), the family's walk_forward_sharpe, and null_percentile. An empty neighbour list fails
    test 3, unless report["no_numeric_params"] is True.

    Returns test1 to test5, each {"value", "threshold", "passes"}, and "eligible", True only if all five pass.
    """
    base = report.get("base")
    base_return, x2_return = _get(base, "total_return"), _get(report.get("x2_costs"), "total_return")
    drawdown = _get(base, "max_drawdown")
    runs = _as_list(report.get("neighbours"))
    returns = [_get(run, "total_return") for run in runs]
    sharpes = [_get(run, "sharpe") for run in runs]
    sharpe_floor = NEIGHBOUR_SHARPE_SHARE * _get(base, "sharpe")
    if runs:
        robust = all(value > 0 for value in returns) and all(value >= sharpe_floor for value in sharpes)
    else:
        robust = report.get("no_numeric_params") is True
    walk_forward = _get(report, "walk_forward_sharpe")
    percentile = _get(report, "null_percentile")
    tests = {
        "test1": _entry({"base": base_return, "x2_costs": x2_return}, 0.0, base_return > 0 and x2_return > 0),
        "test2": _entry(drawdown, MAX_DRAWDOWN, _drawdown_ok(drawdown)),
        "test3": _entry(
            {"neighbours": len(runs), "lowest_total_return": _lowest(returns), "lowest_sharpe": _lowest(sharpes)},
            {"total_return": 0.0, "sharpe": sharpe_floor},
            robust,
        ),
        "test4": _entry(walk_forward, 0.0, walk_forward > 0),
        "test5": _entry(percentile, NULL_SHARE, NULL_SHARE <= percentile <= 1),
    }
    return {**tests, "eligible": all(test["passes"] for test in tests.values())}


def selection_score(base_sharpe, neighbour_sharpes) -> float:
    """The median Sharpe across a variant and its neighbours. Section 11 takes the highest within a family."""
    return float(np.median([_finite(sharpe) for sharpe in (base_sharpe, *neighbour_sharpes)]))


def _regime_half(regimes) -> dict:
    """One period's regimes, from by_regime's flags. A regime that's missing can't be shown not to fail badly."""
    regimes = regimes if isinstance(regimes, dict) else {}
    works, fails_badly = [], []
    for name in REGIMES:
        regime = regimes.get(name)
        regime = regime if isinstance(regime, dict) else {}
        if _is_flag(regime.get("works"), True):
            works.append(name)
        if not _is_flag(regime.get("fails_badly"), False):
            fails_badly.append(name)
    return {"works": works, "fails_badly": fails_badly}


def _regime_half_ok(half: dict) -> bool:
    return len(half["works"]) >= MIN_REGIMES_WORKING and not half["fails_badly"]


def _neighbours_read(eligibility) -> int | None:
    """How many neighbours test 3 read, or None if a hand-made eligibility doesn't say."""
    block = eligibility
    for key in ("test3", "value"):
        block = block.get(key) if isinstance(block, dict) else None
    read = _get(block, "neighbours")
    return None if math.isnan(read) else int(read)


def _holdout_count_ok(on_holdout: int, read: int | None) -> bool:
    # One return for each neighbour test 3 read, and never none, even where it read none. With no count to check
    # against, only the rule against an empty list is left.
    return on_holdout >= 1 and (read is None or on_holdout == read)


def evaluate_gate(candidate: dict) -> dict:
    """G1 to G6 of section 12.

    candidate holds dev and holdout (metrics dicts), dev_regimes and holdout_regimes (from by_regime),
    btc_holdout_sharpe, ew_holdout_sharpe, eligibility (from eligible), neighbour_holdout_returns and dsr_eff.

    Returns G1 to G6, each {"value", "threshold", "passes"}, "passes", True only if all six pass, and dsr_eff. The
    deflated Sharpe isn't part of the gate, so it's read only after passes is settled, and only for "luck_note".
    """
    dev, holdout = candidate.get("dev"), candidate.get("holdout")
    holdout_return, holdout_sharpe = _get(holdout, "total_return"), _get(holdout, "sharpe")
    sharpe_floor = HOLDOUT_SHARPE_SHARE * _get(dev, "sharpe")
    dev_drawdown, holdout_drawdown = _get(dev, "max_drawdown"), _get(holdout, "max_drawdown")
    btc, equal_weight = _get(candidate, "btc_holdout_sharpe"), _get(candidate, "ew_holdout_sharpe")
    eligibility = candidate.get("eligibility")
    section_11 = isinstance(eligibility, dict) and eligibility.get("eligible") is True
    neighbour_returns = [_finite(value) for value in _as_list(candidate.get("neighbour_holdout_returns"))]
    read = _neighbours_read(eligibility)
    dev_half, holdout_half = _regime_half(candidate.get("dev_regimes")), _regime_half(candidate.get("holdout_regimes"))
    gates = {
        "G1": _entry(holdout_return, 0.0, holdout_return > 0),
        "G2": _entry(holdout_sharpe, sharpe_floor, holdout_sharpe >= sharpe_floor),
        "G3": _entry(
            {"dev": dev_drawdown, "holdout": holdout_drawdown},
            MAX_DRAWDOWN,
            _drawdown_ok(dev_drawdown) and _drawdown_ok(holdout_drawdown),
        ),
        "G4": _entry(
            holdout_sharpe,
            {"btc": btc, "equal_weight": equal_weight},
            holdout_sharpe > btc and holdout_sharpe > equal_weight,
        ),
        "G5": _entry(
            {
                "eligible": section_11,
                "neighbours_on_holdout": len(neighbour_returns),
                "lowest_neighbour_holdout_return": _lowest(neighbour_returns),
            },
            {"eligible": True, "neighbours_on_holdout": read or 1, "neighbour_holdout_return": 0.0},
            section_11
            and _holdout_count_ok(len(neighbour_returns), read)
            and all(value > 0 for value in neighbour_returns),
        ),
        "G6": _entry(
            {"dev": dev_half, "holdout": holdout_half},
            {"regimes_working": MIN_REGIMES_WORKING, "regimes_failing_badly": 0},
            _regime_half_ok(dev_half) and _regime_half_ok(holdout_half),
        ),
    }
    passes = all(gate["passes"] for gate in gates.values())
    dsr_eff = _finite(candidate.get("dsr_eff"))
    result = {**gates, "passes": passes, "dsr_eff": dsr_eff}
    if passes and not dsr_eff >= LUCK_DSR:
        result["luck_note"] = LUCK_NOTE
    if holdout_return > 0 and (1 + holdout_return) * (1 - _get(holdout, "open_exit_cost")) <= 1:
        result["exit_note"] = EXIT_NOTE
    return result
