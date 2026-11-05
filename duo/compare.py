"""python -m duo compare: the paper run against the research's own backtest of the same decisions.

Over the paper period, the research engine is re-run on the signal data the decisions used and the targets they made:
research.harness.engine.backtest_weights (or baselines.dca for the DCA fallback), with research/artifacts/cost_model.json
at tier3_taker and GBP 20,000. Its rules: a target decided at the close of bar t fills at the open of bar t+1, at that
size's one-way cost in bps.

It reports, per order, the backtest's assumed fill (the next bar's open plus its cost at that size) against the paper fill
and fee; per day, paper's return against the backtest's, their difference, its mean and tracking error; and the share
of paper fills that were maker.

Where this has to choose, these are the readings taken:
- Prices are the ones logged with each decision: Binance USDT opens and closes. Paper fills are on Kraken's USD books,
  so the per-order gap includes the USDT/USD basis, a few bps at most on a normal day.
- Paper's day runs from one decision to the next, about 00:02 to 00:02 UTC, marked at Kraken mids. The backtest's runs
  from close to close. The few minutes' difference is noise in the tracking error, not a cost.
- A day with no decision (the bot was down) holds the previous target with a band nothing can cross, because that's
  what paper did. The report counts such days.
- A trade's size in GBP is its paper notional at the GBP/USD rate logged with its decision, or the start's rate.
- Where a decision capped a target at the per-asset limit (allow_capped_targets), the backtest replays the capped
  target, which is what paper aimed for, so the tracking error measures execution. The replay of the research's own
  uncapped targets is reported beside it, so the cap's cost shows too.
- A column an earlier decision logged and a later one didn't, the decision logs a few bars of anyway (the runner
  carries them), so the backtest prices its exit at the next open rather than reading a delisting.
- Kill-switch sells have no backtest counterpart, so they carry no assumed fill.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from decimal import Decimal

import numpy as np

from . import sys_research  # noqa: F401  (sets dont_write_bytecode before research imports)
from .config import D
from .db import DB
from research.harness import baselines
from research.harness.data import Panel
from research.harness.engine import backtest_weights

DAY = 86400
HOLD_BAND = 10.0


def _day(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _decisions(db: DB) -> list[dict]:
    rows = []
    for row in db.all("SELECT * FROM sys_decisions ORDER BY day_ts"):
        item = dict(row)
        for key in ("bars", "targets", "bands"):
            item[key] = json.loads(item[key])
        rows.append(item)
    return rows


def _panel(decisions: list[dict], columns: list[str]) -> Panel:
    """Opens and closes over the paper period, from the bars each decision logged. Later logs win."""
    first, last = decisions[0]["day_ts"], decisions[-1]["day_ts"]
    ts = np.arange(first - DAY, last + DAY, DAY, dtype=np.int64)
    opens = np.full((ts.size, len(columns)), np.nan)
    closes = np.full((ts.size, len(columns)), np.nan)
    for decision in decisions:
        for column, bars in decision["bars"].items():
            if column not in columns:
                continue
            c = columns.index(column)
            for k, (o, cl) in enumerate(zip(bars["open"], bars["close"])):
                row = (bars["first_ts"] + k * DAY - ts[0]) // DAY
                if 0 <= row < ts.size and o is not None and cl is not None:
                    opens[row, c], closes[row, c] = o, cl
    nan = np.full(opens.shape, np.nan)
    return Panel("paper_period", ts, tuple(columns), opens, nan, nan, closes, nan, nan)


def compare(db: DB) -> dict:
    """Everything the report and the dashboard show. Empty sections until there's a finished paper day."""
    start = json.loads(db.get("sys_start", "null") or "null")
    decisions = _decisions(db)
    out = {"start": start, "decisions": len(decisions), "days": [], "orders": [], "summary": {}, "notes": []}
    if start is None or not decisions:
        out["notes"].append("No decisions yet.")
        return out
    strategy = sys_research.resolve(start["strategy_id"])
    model = sys_research.load_cost_model()
    capital_gbp = float(start["starting_capital_gbp"])
    columns = sorted({t.get("column") or a for d in decisions for a, t in d["targets"].items()}
                     | {c for d in decisions for c in d["bars"]})
    book_to_column = {a: (t.get("column") or a) for d in decisions for a, t in d["targets"].items()}
    panel = _panel(decisions, columns)
    by_day = {d["day_ts"]: d for d in decisions}
    first_fill = decisions[0]["day_ts"] + DAY

    # Orders: assumed fill against paper fill.
    gbp_default = Decimal(start.get("gbp_usd") or "1")
    fills_maker = fills_all = 0
    notional_maker = notional_all = 0.0
    for order in db.all("SELECT o.*, d.day_ts, d.gbp_usd FROM sys_orders o LEFT JOIN sys_decisions d "
                        "ON d.id = o.decision_id ORDER BY o.id"):
        fills = db.all("SELECT f.* FROM fills f JOIN sys_children c ON c.venue_id = f.venue_id "
                       "WHERE c.order_id = ? AND f.source = 'systematic' ORDER BY f.id", (order["id"],))
        if not fills:
            continue
        qty = sum((D(f["qty"]) for f in fills), Decimal(0))
        gross = sum((D(f["gross"]) for f in fills), Decimal(0))
        fee = sum((D(f["fee"]) for f in fills), Decimal(0))
        for f in fills:
            fills_all += 1
            notional_all += float(f["gross"])
            if f["liquidity"] == "maker":
                fills_maker += 1
                notional_maker += float(f["gross"])
        price = gross / qty
        column = book_to_column.get(order["asset"], order["asset"])
        fill_day = (order["day_ts"] + DAY) if order["day_ts"] is not None and order["source"] != "kill_switch" else None
        assumed_open = cost_bps = None
        if fill_day is not None and column in columns:
            row = (fill_day - int(panel.ts[0])) // DAY
            if 0 <= row < panel.ts.size and not math.isnan(panel.open[row, columns.index(column)]):
                assumed_open = float(panel.open[row, columns.index(column)])
            rate = D(order["gbp_usd"]) if order["gbp_usd"] else gbp_default
            notional_gbp = float(gross / rate) if start["quote"] == "USD" else float(gross)
            cost_bps = model.spot_cost_bps(column, notional_gbp, fill_day, "tier3_taker")
        sign = 1 if order["side"] == "buy" else -1
        slip = None if assumed_open is None else sign * (float(price) / assumed_open - 1) * 1e4
        fee_bps = float(fee / gross * 10000)
        out["orders"].append({
            "order": order["id"],
            "decision_day": _day(order["day_ts"]) if order["day_ts"] and order["source"] != "kill_switch" else None,
            "source": order["source"], "asset": order["asset"], "side": order["side"], "qty": float(qty),
            "notional": float(gross), "assumed_open": assumed_open, "assumed_cost_bps": cost_bps,
            "paper_price": float(price), "paper_fee": float(fee), "paper_fee_bps": fee_bps,
            "price_vs_open_bps": slip, "paper_cost_bps": None if slip is None else slip + fee_bps,
            "gap_bps": None if slip is None or cost_bps is None else slip + fee_bps - cost_bps,
            "maker_share": sum(f["liquidity"] == "maker" for f in fills) / len(fills),
        })

    # Days: the backtest of the same targets against the equity paper logged at each decision.
    research = None
    research_total = None
    if len(decisions) >= 2:
        rows = panel.ts.size

        def replay(key: str):
            target = np.zeros((rows, len(columns)))
            band = np.full((rows, len(columns)), HOLD_BAND)
            held_target = np.zeros(len(columns))
            missing = 0
            for t in range(rows):
                decision = by_day.get(int(panel.ts[t]))
                if decision is None:
                    if panel.ts[t] >= decisions[0]["day_ts"]:
                        missing += 1
                    target[t] = held_target
                    continue
                row = np.zeros(len(columns))
                for asset, entry in decision["targets"].items():
                    if "weight" in entry:
                        row[columns.index(entry.get("column") or asset)] = entry.get(key, entry["weight"])
                band[t] = 0.0  # Columns the decision didn't band have target 0, which always trades whatever the band.
                for asset, value in decision["bands"].items():
                    band[t, columns.index(book_to_column.get(asset, asset))] = value
                target[t] = held_target = row
            target[np.isnan(panel.close)] = 0.0
            return backtest_weights(panel, target, band=band, start_ts=first_fill, capital_gbp=capital_gbp,
                                    cost_model=model, fee_level="tier3_taker"), missing

        missing_days = 0
        if strategy.family == "DCA":
            result = baselines.dca(panel, list(strategy.fixed_assets), weeks=strategy.params["weeks"],
                                   start_ts=first_fill, capital_gbp=capital_gbp, cost_model=model,
                                   fee_level="tier3_taker")
            late = [day for d in decisions for day in json.loads(d["signals"] or "{}").get("late", [])]
            if late:
                out["notes"].append(f"{len(late)} DCA tranche(s) were bought late, at the next decision after their "
                                    f"day passed with none ({', '.join(late)}). The backtest buys each on its day.")
        else:
            result, missing_days = replay("aimed")
            if any("aimed" in entry for d in decisions for entry in d["targets"].values()):
                research_result, _ = replay("weight")
                research = {int(ts): float(r) for ts, r in zip(research_result.ts, research_result.returns)}
                research_total = 0.0
                out["notes"].append("Some targets were above the per-asset limit and paper aimed for the cap. The "
                                    "backtest below replays the capped targets; the research's own targets would have "
                                    "returned the figure in the summary's backtest_total_research_targets.")
        if missing_days:
            out["notes"].append(f"{missing_days} day(s) had no decision, so the backtest holds the previous target "
                                f"on them, as paper did.")
        backtest = {int(ts): float(r) for ts, r in zip(result.ts, result.returns)}
        paper_cum = back_cum = 1.0
        for decision in decisions[1:]:
            fill_day = decision["day_ts"]  # The bar this decision closed on is the day the previous one traded.
            before = by_day.get(fill_day - DAY)
            if before is None or fill_day not in backtest or not before["equity"] or not decision["equity"]:
                continue
            paper = float(D(decision["equity"]) / D(before["equity"]) - 1)
            back = backtest[fill_day]
            paper_cum *= 1 + paper
            back_cum *= 1 + back
            if research is not None:
                research_total = (1 + research_total) * (1 + research.get(fill_day, 0.0)) - 1
            out["days"].append({"day": _day(fill_day), "paper": paper, "backtest": back, "difference": paper - back,
                                "paper_cumulative": paper_cum - 1, "backtest_cumulative": back_cum - 1,
                                "paper_equity": float(D(decision["equity"]))})
    diffs = np.array([d["difference"] for d in out["days"]])
    out["summary"] = {
        "days": len(out["days"]),
        "mean_difference": float(diffs.mean()) if diffs.size else None,
        "tracking_error_daily": float(diffs.std(ddof=1)) if diffs.size > 1 else None,
        "tracking_error_annual": float(diffs.std(ddof=1) * math.sqrt(365)) if diffs.size > 1 else None,
        "paper_total": out["days"][-1]["paper_cumulative"] if out["days"] else None,
        "backtest_total": out["days"][-1]["backtest_cumulative"] if out["days"] else None,
        "fills": fills_all, "maker_fills": fills_maker,
        "maker_share": fills_maker / fills_all if fills_all else None,
        "maker_share_of_notional": notional_maker / notional_all if notional_all else None,
        "fee_level": "tier3_taker", "capital_gbp": capital_gbp,
        "backtest_total_research_targets": research_total,
    }
    return out


def _pct(value, digits=3) -> str:
    return "n/a" if value is None else f"{value * 100:+.{digits}f}%"


def _num(value, digits=2) -> str:
    return "n/a" if value is None else f"{value:,.{digits}f}"


def markdown(report: dict) -> str:
    s, start = report["summary"], report["start"]
    lines = ["# Paper against backtest", ""]
    if not start:
        return "\n".join(lines + ["No paper run yet."]) + "\n"
    lines += [
        f"Strategy `{start['strategy_id']}`, paper since {_day(int(start['started_at']))}, "
        f"{report['decisions']} decision(s). Backtest: `research.harness.engine.backtest_weights` (or `baselines.dca`) "
        f"on the decisions' own signal data and targets, `research/artifacts/cost_model.json` at tier3_taker, "
        f"GBP {_num(s.get('capital_gbp'), 0)}.", "",
        "| Measure | Value |", "|---|---|",
        f"| Finished paper days compared | {s.get('days', 0)} |",
        f"| Paper return over them | {_pct(s.get('paper_total'))} |",
        f"| Backtest return over them | {_pct(s.get('backtest_total'))} |",
        *([f"| Backtest of the research's uncapped targets | {_pct(s.get('backtest_total_research_targets'))} |"]
          if s.get("backtest_total_research_targets") is not None else []),
        f"| Mean daily difference (paper - backtest) | {_pct(s.get('mean_difference'), 4)} |",
        f"| Tracking error, daily | {_pct(s.get('tracking_error_daily'), 4)} |",
        f"| Tracking error, annualised | {_pct(s.get('tracking_error_annual'), 2)} |",
        f"| Paper fills | {s.get('fills', 0)} |",
        f"| Filled as maker | {_pct(s.get('maker_share'), 1) if s.get('maker_share') is not None else 'n/a'} of fills, "
        f"{_pct(s.get('maker_share_of_notional'), 1) if s.get('maker_share_of_notional') is not None else 'n/a'} of notional |",
        "",
    ]
    for note in report["notes"]:
        lines.append(f"- {note}")
    if report["notes"]:
        lines.append("")
    lines += ["## Orders: assumed against actual", "",
              "Assumed: the next bar's open (Binance USDT) and the cost model's one-way cost at that size. Actual: "
              "the paper fill on Kraken and its fee. Costs in bps of notional; a positive gap is paper paying more.", "",
              "| Order | Decision | Side | Asset | Notional | Assumed open | Assumed cost | Paper price | Paper fee | "
              "Price vs open | Paper cost | Gap | Maker |",
              "|---:|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for o in report["orders"]:
        lines.append(
            f"| {o['order']} | {o['decision_day'] or o['source']} | {o['side']} | {o['asset']} | {_num(o['notional'])} | "
            f"{_num(o['assumed_open'], 4)} | {_num(o['assumed_cost_bps'], 1)} | {_num(o['paper_price'], 4)} | "
            f"{_num(o['paper_fee'])} | {_num(o['price_vs_open_bps'], 1)} | {_num(o['paper_cost_bps'], 1)} | "
            f"{_num(o['gap_bps'], 1)} | {o['maker_share'] * 100:.0f}% |")
    if not report["orders"]:
        lines.append("| | No fills yet | | | | | | | | | | | |")
    lines += ["", "## Days", "", "| Day | Paper | Backtest | Difference | Paper cumulative | Backtest cumulative |",
              "|---|---:|---:|---:|---:|---:|"]
    for d in report["days"]:
        lines.append(f"| {d['day']} | {_pct(d['paper'])} | {_pct(d['backtest'])} | {_pct(d['difference'])} | "
                     f"{_pct(d['paper_cumulative'])} | {_pct(d['backtest_cumulative'])} |")
    if not report["days"]:
        lines.append("| Needs two decisions | | | | | |")
    return "\n".join(lines) + "\n"
