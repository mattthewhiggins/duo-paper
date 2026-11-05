"""Writes RESULTS.md at the project root and research/artifacts/variants_table.csv, by code. research/STRATEGY_SPEC.md's
"Reports, by code" section defines it.

It reads only research/artifacts/variants.jsonl, the returns files it names, the selection files select.py writes, and
the rule test count in data/paper.db (Amendment 3), opened read-only. Every figure in the output is computed here from
those. Before writing anything it runs registry.verify_chain, and it refuses if any family and id has two different
sets of params on the record, because then one name would stand for two runs.

The selection files sit outside the hash chain, so nothing in them is taken on trust where the record can show it.
check_selection recomputes from the record and the stored returns, and refuses to report if the file differs: every
metric of every base variant, neighbour and sensitivity it names; tests 1 to 4 and eligibility, through gate.eligible;
each family's walk-forward, through walk_forward on the stored Results with the track's cost model; each score, the
eligible list, the counts and the winner; each break-even fee's steps; and an F5 variant's test 5 where it's the lowest
of its members' percentiles. It also refuses if those lines weren't all made by one code_sha256. What the record
can't show is said in RESULTS.md, not hidden: test 5's null percentiles, which select.py works out off the record
because run_variant takes one Result and a null is 1,000 of them, and BTC's regime labels and F4's halt bars, which
select.py works out from the panels. Editing those in the file still changes the report.

Where the rules leave a choice open, these are the readings taken:

- The params check covers lines of every kind, not only trials: a sensitivity or baseline id that meant two things
  would mislead as much.
- A run's figures are from its latest development line for that family, id and kind, as trial_table does for trials.
  Holdout lines aren't read, so this process never holds a holdout result.
- DSR_raw: n is every trial on the record, on both tracks, from trial_table (base variants, neighbours and F5), plus
  stats.PRIOR_TRIALS, plus the rule test count. trial_srs are those trials' daily Sharpes, the record's annual Sharpe
  over sqrt(365). DSR_eff clusters the same trials, every family and both tracks together. Both are of each family's
  best base variant by after-cost Sharpe on that track, the variant a reader would pick by eye.
- PBO is per family and track, over that family's base variants, which share their days.
- Regime tables use the BTC labels select.py stored from spot_1d, cut to each Result's days.
- "Share of traded notional paying the default" is per family and track, summed over its base variants'
  info["notional_by_flag"]["not_measured"] and info["traded_notional"]. F5 mixes Results and has neither.
- The paper bot's database is read when the report runs, so the count can be higher than at selection time, which only
  makes DSR_raw stricter. A missing database stops the report, rather than leave those runs out.
"""
from __future__ import annotations

import csv
import json
import math
import sqlite3
import urllib.parse
from datetime import datetime, timezone

import numpy as np

from research.harness import gate, registry, stats, walkforward
from research.harness.costs import NOT_MEASURED
from research.harness.data import HOLDOUT_START_TS, _root, _utc
from research.harness.engine import EVAL_START_TS
from research.harness.metrics import summarise
from research.harness.regimes import REGIMES, by_regime
from research.harness.result import Result
from research.strategies import select
from research.strategies.common import DAY, TRACKS, load_cost_model

FAMILY_NAMES = {"F1": "time-series trend", "F2": "cross-sectional momentum", "F3": "funding carry",
                "F4": "mean reversion", "F5": "portfolios of the winners"}
F2_LATE_START = _utc(2020, 1, 1)
TESTS = ("test1", "test2", "test3", "test4", "test5")
COLUMNS = ("cagr", "sharpe", "sortino", "max_drawdown", "calmar", "turnover", "gross_total_return", "total_return",
           "cost_drag", "avg_exposure")
SECTIONS = ("## Summary", "## Families", "## Winners", "## Fallbacks", "## Baselines", "## Data checks",
            "## Appendix: every variant tried")


# Reading.

def _lines() -> list[dict]:
    path = _root() / "research" / "artifacts" / "variants.jsonl"
    return [json.loads(raw) for raw in path.read_bytes().splitlines()] if path.exists() else []


def refuse_reused_ids(lines: list[dict]) -> None:
    """ValueError if a family and id appear with two different params anywhere on the record."""
    seen = {}
    for line in lines:
        text = json.dumps(line["params"], sort_keys=True)
        known = seen.setdefault((line["family"], line["variant_id"]), text)
        if known != text:
            raise ValueError(f"refusing to report: {line['family']}/{line['variant_id']} is on the record with two "
                             f"sets of params, {known} and {text}")


def rule_tests() -> int:
    """state.rule_tests in data/paper.db, read-only, as duo/research.py keeps it. 0 if the key isn't there."""
    path = _root() / "data" / "paper.db"
    if not path.exists():
        raise FileNotFoundError(f"{path} isn't there, and DSR_raw needs its rule test count (Amendment 3)")
    with sqlite3.connect(f"file:{urllib.parse.quote(str(path))}?mode=ro", uri=True) as db:
        row = db.execute("SELECT value FROM state WHERE key = 'rule_tests'").fetchone()
    return int(row[0]) if row else 0


def _selection(track: str):
    path = _root() / "research" / "artifacts" / f"selection_{track}.json"
    return json.loads(path.read_text()) if path.exists() else None


def _slim(result: Result) -> Result:
    """The Result without its weights, which nothing here reads, so thousands of them fit in memory."""
    keep = {key: result.info[key] for key in ("trades", "notional_by_flag", "traded_notional", "open_exit_cost")
            if key in result.info}
    return Result(result.name, result.ts, result.returns, result.gross_returns, result.turnover, result.exposure,
                  result.costs, np.zeros((result.ts.size, 0)), (), keep)


# Formatting. Every number passes through here, so none is typed by hand.

def _number(value, digits=2) -> str:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "n/a"
    return f"{value:,.{digits}f}"


def _pct(value, digits=1) -> str:
    return "n/a" if value is None or not math.isfinite(value) else f"{100 * value:,.{digits}f}%"


def _date(ts) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d")


def _table(header, rows) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(cell).replace("|", "/") for cell in row) + " |" for row in rows]
    return out + [""]


def _test_cell(info, name) -> str:
    if info is None:
        return "-"
    if name in info.get("not_run", []):
        return "not run"
    return "pass" if info["tests"][name]["passes"] else "fail"


# Working out.

def _labels(selection, ts):
    """BTC's four regime labels on these days, from the selection file."""
    stored = selection["regimes"]
    rows = (np.asarray(ts) - stored["first_ts"]) // DAY
    if rows.min() < 0 or rows.max() >= stored["days"]:
        raise ValueError("a Result has days outside the stored regime labels")
    return {name: np.frombuffer(stored[name].encode(), dtype=np.uint8)[rows] == ord("1") for name in REGIMES}


def _matrix(results: dict) -> tuple[list, np.ndarray]:
    """D x N daily returns on the union of their days, NaN where a variant has no row."""
    names = sorted(results)
    days = np.unique(np.concatenate([results[name].ts for name in names]))
    matrix = np.full((days.size, len(names)), np.nan)
    for column, name in enumerate(names):
        matrix[np.searchsorted(days, results[name].ts), column] = results[name].returns
    return names, matrix


def _variant_info(selection, vid):
    """A variant's entry in the selection file, family or F5, or None."""
    if selection is None:
        return None
    for out in [*selection["families"].values(), selection["F5"]]:
        if vid in out.get("variants", {}):
            return out["variants"][vid]
    return None


def _roles(selections) -> dict:
    """(family, id) -> "base" or "neighbour", from the selection files."""
    roles = {}
    for selection in selections.values():
        if selection is None:
            continue
        for family, out in [*selection["families"].items(), ("F5", selection["F5"])]:
            for vid, info in out.get("variants", {}).items():
                roles[(family, vid)] = "base"
                for neighbour in info["neighbours"] or ():
                    roles[(family, neighbour["id"])] = "neighbour"
    return roles


def _regime_counts(selection, result) -> tuple[int, int]:
    table = by_regime(result.returns, _labels(selection, result.ts))
    return sum(bool(r["works"]) for r in table.values()), sum(bool(r["fails_badly"]) for r in table.values())


def _same(track: str, label: str, found, wanted) -> None:
    """Values compared as JSON, the form both files keep them in, with NaN as null."""
    found, wanted = select._plain(found), select._plain(wanted)
    if json.dumps(found, sort_keys=True) != json.dumps(wanted, sort_keys=True):
        raise ValueError(f"refusing to report: selection_{track}.json gives {label} as {json.dumps(found)[:200]}, and "
                         f"the record gives {json.dumps(wanted)[:200]}")


def check_selection(track: str, selection: dict, latest: dict) -> None:
    """ValueError unless what selection_<track>.json says agrees with the record wherever the record can show it."""
    codes = set()

    def line(family, vid, kind):
        found = latest.get((family, vid, kind))
        if found is None:
            raise ValueError(f"refusing to report: selection_{track}.json names {family}/{vid} ({kind}), which isn't "
                             f"on the record")
        codes.add(found["code_sha256"])
        return found

    def costed(family, vid, suffix):
        return line(family, f"{_strip(vid)}__{suffix}{_r(track)}", "sensitivity")["metrics"]

    levels = list(TRACKS[track]["sensitivities"])
    _same(track, "the fee levels", selection["sensitivities"], levels)
    model = load_cost_model(track)
    outs = [*selection["families"].items(), *([("F5", selection["F5"])] if "variants" in selection["F5"] else [])]
    percentiles = {}
    winners = {}
    for family, out in outs:
        for vid, info in out["variants"].items():
            if info["null"] is not None and info["null"].get("seeds") is not None:
                percentiles[_strip(vid)] = info["null"]["percentile"]
    for family, out in outs:
        variants = out["variants"]
        # Only the columns each ever held, as select.py hands walk_forward, which reads nothing else.
        results = {vid: select._slim(registry.load_result(line(family, vid, "trial"))) for vid in variants}
        wf = walkforward.walk_forward(results, cost_model=model, fee_level=selection["fee_level"],
                                      charge_first_entry=True)
        found = out["walk_forward"]
        _same(track, f"{family}'s walk-forward",
              [found["sharpe"], found["gap"], found["best_in_sample"], found["picks"], found["switch_costs"]],
              [wf["sharpe"], wf["gap"], list(wf["best_in_sample"]), [[int(t), name] for t, name in wf["picks"]],
               wf["switch_costs"]])
        eligible, scores, counts = [], {}, {f"test{k}": 0 for k in range(1, 6)}
        for vid, info in variants.items():
            base = line(family, vid, "trial")["metrics"]
            _same(track, f"{vid}'s metrics", info["metrics"], base)
            _same(track, f"{vid}'s costs x2", info["x2"], costed(family, vid, "x2"))
            for level in levels:
                _same(track, f"{vid}'s {level}", info["sensitivities"][level], costed(family, vid, level))
            neighbours = None
            if info["neighbours"] is not None:
                neighbours = []
                for neighbour in info["neighbours"]:
                    metrics = line(family, neighbour["id"], "trial")["metrics"]
                    _same(track, f"{neighbour['id']}'s metrics", neighbour["metrics"], metrics)
                    for suffix, figures in neighbour.get("sensitivities", {}).items():
                        _same(track, f"{neighbour['id']}'s {suffix}", figures,
                              costed(family, neighbour["id"], suffix))
                    neighbours.append(metrics)
            null = info["null"]
            if null is not None and null.get("seeds") is None:  # An F5 variant's test 5 from its members.
                members = null["members"]
                _same(track, f"{vid}'s members", sorted(members), sorted(info["params"]["members"]))
                _same(track, f"{vid}'s member percentiles", members,
                      {member: percentiles.get(member) for member in members})
                _same(track, f"{vid}'s null percentile", null["percentile"], min(members.values()))
            tests = gate.eligible({"base": base, "x2_costs": costed(family, vid, "x2"),
                                   "neighbours": neighbours or [], "walk_forward_sharpe": wf["sharpe"],
                                   "null_percentile": None if null is None else null["percentile"]})
            _same(track, f"{vid}'s tests", info["tests"], tests)
            _same(track, f"{vid}'s tests not run", info["not_run"],
                  [name for name, missing in (("test3", neighbours is None), ("test5", null is None)) if missing])
            score = None
            if tests["eligible"]:
                score = gate.selection_score(base["sharpe"], [n["sharpe"] for n in neighbours])
                eligible.append(vid)
                scores[vid] = score
            _same(track, f"{vid}'s score", info["score"], score)
            for k in range(1, 6):
                counts[f"test{k}"] += bool(tests[f"test{k}"]["passes"])
        winner = min(eligible, key=lambda vid: (-scores[vid], vid)) if eligible else None
        _same(track, f"{family}'s winner", out["winner"], winner)
        _same(track, f"{family}'s eligible", sorted(out["eligible"]), sorted(eligible))
        _same(track, f"{family}'s counts", {key: out["counts"][key] for key in [*counts, "eligible", "variants"]},
              {**counts, "eligible": len(eligible), "variants": len(variants)})
        if winner:
            winners[family] = winner
    _same(track, "the winners", selection["winners"], winners)
    for vid, found in selection["break_even"].items():
        family = "BASE" if vid.startswith("BASE") else vid.split("__")[0]
        steps = {}
        for bps, value in found["tried"]:
            figures = costed(family, vid, f"fee_bps={select._text(bps)}")
            _same(track, f"{vid}'s return at a fee of {bps} bps", value, figures["total_return"])
            steps[bps] = figures["total_return"]
        def above(bps):
            if bps not in steps:
                raise ValueError(f"refusing to report: {vid}'s break-even names {bps} bps, a fee it didn't try")
            return steps[bps] is not None and steps[bps] > 0
        if found["low_bps"] is not None and not above(found["low_bps"]):
            raise ValueError(f"refusing to report: {vid}'s break-even says above 0 at {found['low_bps']} bps")
        if found["high_bps"] is not None and above(found["high_bps"]):
            raise ValueError(f"refusing to report: {vid}'s break-even says not above 0 at {found['high_bps']} bps")
    if len(codes) > 1:
        raise ValueError(f"refusing to report: selection_{track}.json rests on runs from {len(codes)} versions of the "
                         f"code")


def build(say=print) -> tuple:
    """Writes RESULTS.md and variants_table.csv. Returns their paths."""
    registry.verify_chain()
    lines = _lines()
    refuse_reused_ids(lines)
    latest = {}
    for line in lines:
        if line["split"] == "dev":
            latest[(line["family"], line["variant_id"], line["kind"])] = line
    selections = {track: _selection(track) for track in TRACKS}
    for track, selection in selections.items():
        if selection is not None:
            say(f"report: checking selection_{track}.json against the record")
            check_selection(track, selection, latest)
    tests_run = rule_tests()
    prior = stats.PRIOR_TRIALS + tests_run
    trials = registry.trial_table()
    say(f"report: loading {len(trials)} trial results")
    results = {(line["family"], line["variant_id"]): _slim(registry.load_result(line)) for line in trials}
    roles = _roles(selections)
    trial_srs = [(line["metrics"]["sharpe"] if line["metrics"]["sharpe"] is not None else math.nan) / math.sqrt(365)
                 for line in trials]
    names, matrix = _matrix({f"{family}/{vid}": result for (family, vid), result in results.items()})
    metrics = lambda family, vid, kind="trial": (latest.get((family, vid, kind)) or {}).get("metrics") or {}

    md = ["# Results", "",
          f"Written by `research/strategies/report.py` from `research/artifacts/variants.jsonl` ({len(lines)} lines), "
          f"the stored returns, `selection_K.json` and `selection_R.json`, and `data/paper.db`. Development data only, "
          f"before {_date(HOLDOUT_START_TS)}. No figure here was typed by hand.", "",
          "Every outcome taken from the selection files was recomputed from the record and the stored returns before "
          "this was written, and agreed: metrics, sensitivities, tests 1 to 4, scores, walk-forwards, winners and "
          "break-even fees. Three things the record doesn't hold are taken from the selection files as they stand: "
          "test 5's null percentiles, which are worked out off the record because the registry records one Result "
          "per run and a null is 1,000 runs (preregistration section 6 asks for every run on real data to be on the "
          "record, so this is an open conflict for the owner), BTC's regime labels, and F4's halt bars.", ""]

    # Families: the statistics first, because the summary quotes them.
    family_rows, dsr = [], {}
    for track, selection in selections.items():
        if selection is None:
            continue
        for family, out in [*selection["families"].items(), ("F5", selection["F5"])]:
            if "variants" not in out:
                continue
            base = {vid: results[(family, vid)] for vid in out["variants"] if (family, vid) in results}
            best = out["top_by_sharpe"][0] if out["top_by_sharpe"] else None
            pbo = math.nan
            if len(base) >= 2:
                pbo = stats.pbo_cscv(np.column_stack([base[vid].returns for vid in sorted(base)]))["pbo"]
            raw = eff = math.nan
            if best in base:
                raw = stats.dsr_raw(base[best].returns, trial_srs, prior_trials=prior)
                eff = stats.dsr_eff(base[best].returns, names, matrix)
            dsr[(track, family)] = (best, raw, eff)
            counts, wf = out["counts"], out["walk_forward"]
            not_run_3 = sum("test3" in v["not_run"] for v in out["variants"].values())
            not_run_5 = sum("test5" in v["not_run"] for v in out["variants"].values())
            family_rows.append([
                track, family, counts["variants"], counts["test1"], counts["test2"],
                f"{counts['test3']} ({not_run_3} not run)", counts["test4"], f"{counts['test5']} ({not_run_5} not run)",
                counts["eligible"], _number(wf["sharpe"]), _number(wf["gap"]), _number(pbo), best or "n/a",
                _number(metrics(family, best).get("sharpe")), _number(raw, 3), _number(eff, 3)])

    # Summary.
    md += ["## Summary", ""]
    candidates = {}
    for track, selection in selections.items():
        if selection is None:
            md += [f"Track {track} hasn't been run: there's no `selection_{track}.json`.", ""]
            continue
        md += [f"**Track {track}** ({selection['fee_level']}).", ""]
        for family in ("F1", "F2", "F3", "F4", "F5"):
            out = selection["F5"] if family == "F5" else selection["families"].get(family)
            if out is None:
                md.append(f"- {family}: not run on this track.")
            elif "skipped" in out:
                md.append(f"- {family}: not made, because {out['skipped']}.")
            elif out["winner"]:
                vid = out["winner"]
                info = out["variants"][vid]
                md.append(f"- {family}: **{vid}** passed all five tests of section 11, {out['counts']['eligible']} of "
                          f"{out['counts']['variants']} eligible. Sharpe {_number(info['metrics']['sharpe'])}, median "
                          f"with its neighbours {_number(info['score'])}.")
                strategy = vid[: -len("__R")] if vid.endswith("__R") else vid
                candidates.setdefault(strategy, []).append(track)
            else:
                md.append(f"- {family}: nothing passed section 11 ({out['counts']['variants']} variants).")
        md.append("")
    md += ["**Candidates that would go to the holdout**, each once, in one batch:", ""]
    if candidates:
        for strategy, tracks in sorted(candidates.items()):
            notes = []
            if tracks == ["R"]:
                notes.append("chosen on track R only: Revolut X's costs, whose fee couldn't be read at source")
            if strategy.startswith("F3") or "+F3__" in strategy:
                notes.append("carry needs perps, which UK retail clients can't use, and liquidation isn't modelled")
            md.append(f"- {strategy}, chosen on track {' and '.join(tracks)}" + (f" ({'; '.join(notes)})" if notes
                                                                                    else "") + ".")
    else:
        md.append("- None. Nothing passed section 11 on either track.")
    md += ["", "**Fallbacks**, always reported:", ""]
    for track, selection in selections.items():
        if selection is None:
            continue
        vid = selection["fallbacks"]["FALLBACK_TREND"]
        info = _variant_info(selection, vid)
        tests = ", ".join(f"{name} {_test_cell(info, name)}" for name in TESTS)
        dca = selection["fallbacks"]["FALLBACK_DCA"]
        md.append(f"- Track {track}: FALLBACK_TREND {vid}, Sharpe {_number(metrics('F1', vid).get('sharpe'))}, "
                  f"after-cost return {_pct(metrics('F1', vid).get('total_return'))}, section 11: {tests}. "
                  f"FALLBACK_DCA {dca}, Sharpe {_number(metrics('BASE', dca, 'baseline').get('sharpe'))}, after-cost "
                  f"return {_pct(metrics('BASE', dca, 'baseline').get('total_return'))}.")
    md += ["", "**Baselines** from the evaluation start, after costs:", ""]
    for track, selection in selections.items():
        if selection is None:
            continue
        for vid, info in sorted(selection["baselines"].items()):
            if info["kind"] == "baseline" and info["params"]["start"] == _date(EVAL_START_TS):
                m = metrics("BASE", vid, "baseline")
                md.append(f"- Track {track}, {vid}: Sharpe {_number(m.get('sharpe'))}, after-cost return "
                          f"{_pct(m.get('total_return'))}, max drawdown {_pct(m.get('max_drawdown'))}.")
    md += ["", "**What is already contaminated** (section 2 and Amendment 3 of the preregistration):", "",
           f"- Before the preregistration, this project ran {stats.PRIOR_TRIALS:,} rule backtests on BTC, ETH and SOL "
           f"over dates that include the holdout, and the researcher saw year-by-year results and buy-and-hold returns "
           f"across the holdout period.",
           f"- The researcher is an LLM whose general knowledge of crypto markets runs past {_date(HOLDOUT_START_TS)}, "
           f"into the holdout.",
           f"- The paper bot's agents have made {tests_run:,} test_rule calls on Coinbase history that includes the "
           f"holdout (data/paper.db, read now).",
           f"- So the holdout is untouched only in a narrow sense, and only forward paper trading is a clean test. "
           f"DSR_raw counts all {prior:,} of those earlier runs.", ""]

    # Families.
    md += ["## Families", "",
           f"Trials on the record, both tracks: {len(trials):,}. DSR_raw's n adds {stats.PRIOR_TRIALS:,} earlier runs and "
           f"{tests_run:,} rule tests, so n = {len(trials) + prior:,}. DSR_eff clusters all {len(names):,} trials of "
           f"every family on both tracks. Amendment 6 item 7: DSR_eff doesn't deflate for choosing the best inside a "
           f"cluster, so with few clusters pure noise clears it far more often than its nominal rate. Read it beside "
           f"DSR_raw, never alone. Tests 3 and 5 are \"not run\" for a variant that failed an earlier test.", ""]
    md += _table(["Track", "Family", "Variants", "T1", "T2", "T3", "T4", "T5", "Eligible", "WF Sharpe", "WF gap", "PBO",
                  "Best by Sharpe", "Its Sharpe", "DSR_raw", "DSR_eff"], family_rows)
    md += ["F3 starts on its own window (Amendment 5 item 7) and is flagged: UK retail clients can't use crypto "
           "derivatives, and liquidation isn't modelled.", ""]

    # Winners.
    md += ["## Winners", ""]
    any_winner = False
    for track, selection in selections.items():
        if selection is None:
            continue
        for family, vid in sorted(selection["winners"].items()):
            any_winner = True
            md += _variant_block(track, selection, family, vid, results, metrics, f"Winner, {family}")
    if not any_winner:
        md += ["No variant passed section 11 on either track.", ""]

    # Fallbacks.
    md += ["## Fallbacks", ""]
    for track, selection in selections.items():
        if selection is None:
            continue
        vid = selection["fallbacks"]["FALLBACK_TREND"]
        md += _variant_block(track, selection, "F1", vid, results, metrics, "FALLBACK_TREND")
        dca = selection["fallbacks"]["FALLBACK_DCA"]
        md += [f"### FALLBACK_DCA, track {track}: {dca}", ""]
        rows = [["base", *(_cost_row(metrics("BASE", dca, "baseline")))]]
        for suffix in ("x2", *selection["sensitivities"]):
            rows.append([suffix, *_cost_row(metrics("BASE", f"{_strip(dca)}__{suffix}{_r(track)}", "sensitivity"))])
        md += _table(["Costs", "Sharpe", "After-cost return", "Max drawdown"], rows)
        md += [_break_even_text(selection["break_even"].get(dca)), ""]

    # Baselines.
    md += ["## Baselines", "", "Each from the start of whatever it's compared with: 2018-07-01, or F3's own window.", ""]
    rows = []
    for track, selection in selections.items():
        if selection is None:
            continue
        for vid, info in sorted(selection["baselines"].items()):
            m = metrics("BASE", vid, info["kind"])
            rows.append([track, vid, info["kind"], *(_number(m.get(key), 3) for key in COLUMNS)])
    md += _table(["Track", "Baseline", "Kind", *COLUMNS], rows)

    # Data checks.
    md += ["## Data checks", "", "Three lines the data check asked for. None of them selects anything.", "",
           f"**F2 on TOP30 from {_date(F2_LATE_START)}**, beside the same stored returns from the evaluation start:", ""]
    rows = []
    for (family, vid), result in sorted(results.items()):
        info = _variant_info(selections.get(_track(vid)), vid)
        if family == "F2" and roles.get((family, vid)) == "base" and info and info["params"].get("n") == 30:
            whole, late = summarise(result), summarise(result, start_ts=F2_LATE_START)
            rows.append([vid, _number(whole["sharpe"]), _pct(whole["cagr"]), _number(late["sharpe"]),
                         _pct(late["cagr"])])
    md += _table(["Variant", "Sharpe from start", "CAGR from start", f"Sharpe from {_date(F2_LATE_START)}",
                  f"CAGR from {_date(F2_LATE_START)}"], rows) if rows else ["No F2 TOP30 variant on the record.", ""]
    md += ["**F4 entries at the first bar after a halt of 2 hours or more**, which can open at a price from before the "
           "halt:", ""]
    rows = []
    for (family, vid), result in sorted(results.items()):
        selection = selections.get(_track(vid))
        info = _variant_info(selection, vid)
        if family == "F4" and roles.get((family, vid)) == "base" and info:
            halts = selection["f4_halts"][info["params"]["bar"]]
            trades = result.info.get("trades", [])
            flagged = sum(trade["entry_ts"] in set(halts.get(trade["asset"], ())) for trade in trades)
            rows.append([vid, len(trades), flagged])
    md += _table(["Variant", "Entries", "After a halt"], rows) if rows else ["No F4 variant on the record.", ""]
    md += ["**Costs that aren't measured**: the share of traded notional paying the 50 bps default, from each base "
           "variant's notional_by_flag and traded_notional:", ""]
    rows = []
    for track, selection in selections.items():
        if selection is None:
            continue
        for family in selection["families"]:
            default = traded = 0.0
            for vid in selection["families"][family]["variants"]:
                info = results[(family, vid)].info
                default += info.get("notional_by_flag", {}).get(NOT_MEASURED, 0.0)
                traded += info.get("traded_notional", 0.0)
            rows.append([track, family, _number(traded, 1), _pct(default / traded if traded else math.nan)])
    md += _table(["Track", "Family", "Traded notional (sum of weights)", "Share at the default"], rows)

    # Appendix and CSV.
    md += ["## Appendix: every variant tried", "",
           "Every trial on the record, base variants and neighbours, on both tracks, after costs at base costs. T1 to T5 "
           "are section 11's tests (base variants only). Regimes: how many of BTC's four regimes work, and how many "
           "fail badly.", ""]
    rows, csv_rows = [], []
    for (family, vid), result in sorted(results.items()):
        selection = selections.get(_track(vid))
        info = _variant_info(selection, vid)
        m = metrics(family, vid)
        works, fails = _regime_counts(selection, result) if selection else (None, None)
        cells = [_test_cell(info, name) for name in TESTS]
        rows.append([family, vid, roles.get((family, vid), "other"),
                     *(_pct(m.get(key)) if key in ("cagr", "max_drawdown", "gross_total_return", "total_return",
                                                    "cost_drag") else _number(m.get(key)) for key in COLUMNS),
                     *cells, "yes" if info and info["tests"]["eligible"] else "-",
                     "n/a" if works is None else f"{works}/{fails}"])
    md += _table(["Family", "Variant", "Role", "CAGR", "Sharpe", "Sortino", "Max DD", "Calmar", "Turnover", "Gross",
                  "After cost", "Cost drag", "Avg exposure", "T1", "T2", "T3", "T4", "T5", "Eligible",
                  "Regimes works/fails"], rows)
    md += ["### Sensitivities of every variant tried", "",
           "Section 5's always-reported costs, for every trial on the record: Sharpe and after-cost return at base "
           "costs x2 and at each of the track's other fee levels. The break-even fee is the fee in bps between the "
           "highest at which after-cost return is above 0 and the lowest at which it isn't, for the variants it's "
           "computed for (the top 3 by Sharpe in each family, every winner and both fallbacks), and \"-\" for the "
           "rest.", ""]
    rows = []
    for (family, vid), result in sorted(results.items()):
        track = _track(vid)
        cells = []
        for suffix in ("x2", *TRACKS[track]["sensitivities"]):
            m = metrics(family, f"{_strip(vid)}__{suffix}{_r(track)}", "sensitivity")
            cells.append(f"{_number(m.get('sharpe'))} / {_pct(m.get('total_return'))}" if m else "not run")
        rows.append([family, vid, track, *cells[:1], " ; ".join(f"{level} {cell}" for level, cell in
                                                                zip(TRACKS[track]["sensitivities"], cells[1:])),
                     _break_even_cell((selections.get(track) or {}).get("break_even", {}).get(vid))])
    md += _table(["Family", "Variant", "Track", "x2: Sharpe / return", "Fee levels: Sharpe / return", "Break-even fee"],
                 rows)
    md += ["### Regimes of every variant tried", "",
           "BTC's four regimes (section 8), for every trial on the record: days, annualised after-cost return and "
           "Sharpe in each, and whether it works or fails badly there (section 12's G6 rule).", ""]
    rows = []
    for (family, vid), result in sorted(results.items()):
        selection = selections.get(_track(vid))
        if selection is None:
            continue
        table = by_regime(result.returns, _labels(selection, result.ts))
        rows.append([family, vid, *(_regime_cell(table[name]) for name in REGIMES)])
    md += _table(["Family", "Variant", *REGIMES], rows)
    for (family, vid, kind), line in sorted(latest.items()):
        selection = selections.get(_track(vid))
        info = _variant_info(selection, vid) if kind == "trial" else None
        result = results.get((family, vid)) if kind == "trial" else None
        regime = _regime_counts(selection, result) if result is not None and selection else (None, None)
        flags = result.info if result is not None else {}
        traded = flags.get("traded_notional")
        csv_rows.append({
            "family": family, "variant_id": vid, "kind": kind, "track": line["params"].get("track", ""),
            "role": roles.get((family, vid), "other") if kind == "trial" else kind,
            "params": json.dumps(line["params"], sort_keys=True), "seq": line["seq"],
            **{key: line["metrics"].get(key) for key in (*COLUMNS, "days", "total_costs", "open_exit_cost")},
            **{name: _test_cell(info, name) if info else "" for name in TESTS},
            "eligible": "" if info is None else info["tests"]["eligible"],
            "null_percentile": (info.get("null") or {}).get("percentile") if info else "",
            "score": info.get("score") if info else "",
            "regimes_working": "" if regime[0] is None else regime[0],
            "regimes_failing_badly": "" if regime[1] is None else regime[1],
            "default_cost_share": "" if not traded else flags.get("notional_by_flag", {}).get(NOT_MEASURED, 0.0) / traded,
            **_break_even_columns((selection or {}).get("break_even", {}).get(vid) if kind in ("trial", "baseline")
                                  else None),
        })

    results_md = _root() / "RESULTS.md"
    results_md.write_text("\n".join(md) + "\n")
    table = _root() / "research" / "artifacts" / "variants_table.csv"
    with table.open("w", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=list(csv_rows[0]) if csv_rows else ["family"])
        writer.writeheader()
        writer.writerows(csv_rows)
    say(f"report: wrote {results_md} and {table}")
    return results_md, table


def _r(track: str) -> str:
    return "__R" if track == "R" else ""


def _strip(vid: str) -> str:
    return vid[: -len("__R")] if vid.endswith("__R") else vid


def _track(vid: str) -> str:
    return "R" if vid.endswith("__R") else "K"


def _cost_row(m: dict) -> list:
    return [_number(m.get("sharpe")), _pct(m.get("total_return")), _pct(m.get("max_drawdown"))]


def _break_even_text(found) -> str:
    if not found:
        return "Break-even fee: not computed."
    if found.get("note"):
        bound = found["low_bps"] if found["low_bps"] is not None else found["high_bps"]
        return f"Break-even fee: {found['note']} ({_number(bound, 1)} bps)."
    return (f"Break-even fee: after-cost return is above 0 at a fee of {_number(found['low_bps'], 1)} bps and not at "
            f"{_number(found['high_bps'], 1)} bps.")


def _break_even_cell(found) -> str:
    if not found:
        return "-"
    if found.get("note"):
        bound = found["low_bps"] if found["low_bps"] is not None else found["high_bps"]
        return f"{found['note']} ({_number(bound, 1)} bps)"
    return f"{_number(found['low_bps'], 1)} to {_number(found['high_bps'], 1)} bps"


def _break_even_columns(found) -> dict:
    found = found or {}
    return {"break_even_low_bps": found.get("low_bps", ""), "break_even_high_bps": found.get("high_bps", ""),
            "break_even_note": found.get("note", "")}


def _regime_cell(figures) -> str:
    verdict = "works" if figures["works"] else "fails badly" if figures["fails_badly"] else "neither"
    return (f"{figures['days']} d, {_pct(figures['annual_return'])}, Sharpe {_number(figures['sharpe'])}, "
            f"{verdict}")


def _variant_block(track, selection, family, vid, results, metrics, title) -> list[str]:
    """A variant's figures, its section 11 tests, its regime table, its sensitivities and its break-even fee."""
    info = _variant_info(selection, vid)
    md = [f"### {title}, track {track}: {vid}", ""]
    m = metrics(family, vid)
    md += _table(["Metric", "Value"], [[key, _number(m.get(key), 3)] for key in (*COLUMNS, "open_exit_cost")])
    if info:
        rows = []
        for name in TESTS:
            test = info["tests"][name]
            rows.append([name, _test_cell(info, name), json.dumps(test["value"]), json.dumps(test["threshold"])])
        md += _table(["Test", "Outcome", "Value", "Threshold"], rows)
        null = info.get("null")
        if null:
            md += [f"Null: {null['how']}. Percentile {_number(null['percentile'], 3)} against "
                   f"{_number(gate.NULL_SHARE, 2)} needed" + (f", over {null['seeds']} seeds." if null["seeds"] else
                                                               "."), ""]
    result = results.get((family, vid))
    if result is not None:
        table = by_regime(result.returns, _labels(selection, result.ts))
        md += _table(["Regime", "Days", "Annual return", "Sharpe", "Works", "Fails badly"],
                     [[name, r["days"], _pct(r["annual_return"]), _number(r["sharpe"]), r["works"], r["fails_badly"]]
                      for name, r in table.items()])
    rows = [["base", *_cost_row(m)], ["x2", *_cost_row(metrics(family, f"{_strip(vid)}__x2{_r(track)}",
                                                                 "sensitivity"))]]
    for level in selection["sensitivities"]:
        rows.append([level, *_cost_row(metrics(family, f"{_strip(vid)}__{level}{_r(track)}", "sensitivity"))])
    md += _table(["Costs", "Sharpe", "After-cost return", "Max drawdown"], rows)
    md += [_break_even_text(selection["break_even"].get(vid)), ""]
    return md
