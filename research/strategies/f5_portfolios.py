"""F5: portfolios of the family winners, mixed from their after-cost daily returns. research/STRATEGY_SPEC.md defines it.

Only made when a track has at least two family winners. Members are the winners, their returns are mixed with equal or
inverse-volatility shares, and the shares are set again on the first day of each UTC month, paying
abs(share change) x the one-way cost of BTC at that size from each side's share, at the track's fee.

Where the spec leaves a choice open, these are the readings taken:

- Every F5 variant on a track starts on the latest first day among all that track's winners, so they compare over the
  same days, as F3's variants do (Amendment 5 item 7), and the family walk-forward can line them up. A winner from F3
  moves every F5 variant's start to F3's.
- A member already in position on F5's first day, which is every member that started earlier, pays to enter that
  position from cash on F5's first day: its weights at the close before, times F5's share, at each asset's one-way
  cost, and a carry member's perp leg at the perp's fee and half-spread. Without it F5 would start invested for free,
  which Amendment 6 item 5 rules out for the walk-forward. This can only lower a result.
- invvol's shares are 1 / the std (ddof=1) of each member's own last 90 daily returns, ending at the close before
  the rebalance day, scaled to sum to 1. A member's returns from before F5's first day count, because they're known at
  that close: "until 90 days exist" counts the member's own days. If any member has fewer than 90, or its std is 0 or
  can't be computed, that rebalance uses equal shares, because 1/0 would put everything in one member.
- The rebalance interval is a parameter, months, because Amendment 2 item 3 varies every numeric parameter, the
  rebalance interval by name, and the preregistration wins over STRATEGY_SPEC.md's "F5's neighbours are the 90 days".
  Monthly is months = 1, whose only neighbour is 2 (20% of 1 rounds back to 1, and never below 1). With months = k the
  shares are set on the first day of every k-th UTC month, counted from F5's first month. So the equal mixes have a
  neighbour too, and test 3 isn't passed for want of one.
- The day's return is the members' after-cost returns weighted by the shares at the open, less the rebalance charge
  taken off the open's equity as the engines take costs. Shares then drift with each member's return.
- Weights are each member's weights times its share, summed per asset. A carry member's perp leg is a column of its own
  named "<asset>:perp", so the walk-forward charges for moving it. The cost model has no book of that name and prices it
  at the 50 bps default, far above the perp's own cost: stricter than leaving it out, which would make it free.
- info carries open_exit_cost as the members' own, weighted by their shares at the end, and rebalance_costs as the sum
  of F5's own charges, entry charges included. It doesn't carry notional_by_flag: a share of each member's totals can't
  be weighted day by day from those totals.
"""
from __future__ import annotations

import itertools

import numpy as np

from research.harness.engine import CAPITAL_GBP
from research.harness.result import Result

FAMILY = "F5"
WINDOW = 90
MAX_VARIANTS = 10
MONTHS = 1
NUMERIC = {"window": {"integer": True}, "months": {"integer": True}}
PERP = ":perp"  # The suffix of a carry member's perp-leg column.


def variants(winner_ids) -> list[dict]:
    """Sets of two or more winners, largest first and then by name, each with equal and then invvol, at most 10.

    winner_ids are the winners' base ids (without __R), and a set lists its members by name.
    """
    names = sorted(winner_ids)
    out = []
    for size in range(len(names), 1, -1):
        for members in itertools.combinations(names, size):
            out.append({"members": list(members), "weighting": "equal", "months": MONTHS})
            out.append({"members": list(members), "weighting": "invvol", "window": WINDOW, "months": MONTHS})
    return out[:MAX_VARIANTS]


def parts(params: dict) -> list[str]:
    """The id's parts: the members joined by "+", then the weighting."""
    return ["+".join(params["members"]), params["weighting"]]


def numeric(params: dict) -> dict:
    """The parameters gate.neighbours varies: the rebalance interval, and invvol's window."""
    return {key: value for key, value in params.items() if key in ("weighting", "window", "months")}


def _bps(cost_model, asset, notional_gbp, ts, fee_level, fee_bps) -> float:
    """One trade's one-way cost in bps, with fee_bps in place of the fee if given, as the engines do it."""
    detail = cost_model.spot_cost_detail(asset, notional_gbp, ts, fee_level)
    return detail.total_bps if fee_bps is None else fee_bps + detail.fx_bps + detail.spread_impact_bps


def _perp_bps(cost_model, ts) -> float:
    return cost_model.perp_fee_bps + cost_model.perp_half_spread_bps * cost_model.multiplier(ts)


def _shares(params: dict, members, ends) -> np.ndarray:
    """Target shares at a rebalance whose day is row ends[m] of member m, from each member's rows before it."""
    count = len(members)
    equal = np.full(count, 1.0 / count)
    if params["weighting"] == "equal":
        return equal
    if params["weighting"] != "invvol":
        raise ValueError(f"unknown weighting {params['weighting']!r}")
    window = params["window"]
    if min(ends) < window:
        return equal
    std = np.array([member.returns[end - window : end].std(ddof=1) for member, end in zip(members, ends)])
    if not (np.isfinite(std).all() and (std > 0).all()):
        return equal
    raw = 1.0 / std
    return raw / raw.sum()


def mix(members, params: dict, *, start_ts: int, cost_model, fee_level: str, cost_scale: float = 1.0, fee_bps=None,
        capital_gbp: float = CAPITAL_GBP, name: str = "") -> Result:
    """One F5 variant from its members' Results, in params["members"] order, over the days from start_ts."""
    if len(members) != len(params["members"]) or len(members) < 2:
        raise ValueError("F5 needs a Result for each of its two or more members")
    firsts = [int(np.searchsorted(member.ts, start_ts)) for member in members]
    ts = members[0].ts[firsts[0] :]
    for member, first in zip(members, firsts):
        if not np.array_equal(member.ts[first:], ts):
            raise ValueError(f"{member.name} doesn't have one row for each of F5's days from {start_ts}")
    if not ts.size or ts[0] != start_ts:
        raise ValueError(f"F5's members have no row on its first day, {start_ts}")
    cut = lambda key: np.stack([getattr(m, key)[f:] for m, f in zip(members, firsts)], axis=1)
    returns, gross, turnover, exposure, costs = (cut(key) for key in ("returns", "gross_returns", "turnover",
                                                                       "exposure", "costs"))
    carry = [bool(member.info.get("carry")) for member in members]
    assets = []
    for member, is_carry in zip(members, carry):
        for asset in (*member.assets, *(f"{a}{PERP}" for a in member.assets if is_carry)):
            if asset not in assets:
                assets.append(asset)
    place = {asset: column for column, asset in enumerate(assets)}

    days = ts.size
    out = {key: np.zeros(days) for key in ("returns", "gross_returns", "turnover", "exposure", "costs")}
    weights = np.zeros((days, len(assets)))
    months = ts.astype("datetime64[s]").astype("datetime64[M]").astype(np.int64)
    rebalance = np.r_[False, (months[1:] != months[:-1]) & ((months[1:] - months[0]) % params["months"] == 0)]
    share = np.zeros(len(members))
    equity, charged = 1.0, 0.0
    for row in range(days):
        stamp = int(ts[row])
        cost = 0.0
        if row == 0:
            share = _shares(params, members, firsts)
            # Members already in position pay to enter it from cash, from their weights at the close before F5's day.
            for m, (member, first) in enumerate(zip(members, firsts)):
                if first == 0:
                    continue
                for a, weight in enumerate(member.weights[first - 1].tolist()):
                    if weight > 0.0:
                        size = weight * share[m]
                        bps = _bps(cost_model, member.assets[a], size * capital_gbp, stamp, fee_level, fee_bps)
                        cost += size * (bps + (_perp_bps(cost_model, stamp) if carry[m] else 0.0)) / 1e4
            cost *= cost_scale
        elif rebalance[row]:
            target = _shares(params, members, [first + row for first in firsts])
            change = np.abs(target - share)
            for m in np.flatnonzero(change).tolist():
                size = float(change[m])
                cost += size * _bps(cost_model, "BTC", size * equity * capital_gbp, stamp, fee_level, fee_bps) / 1e4
            cost *= cost_scale
            out["turnover"][row] += float(change.sum())
            share = target
        day = float(share @ returns[row])
        kept = max(0.0, 1.0 - cost)
        out["returns"][row] = (1.0 + day) * kept - 1.0
        out["gross_returns"][row] = float(share @ gross[row])
        out["costs"][row] = cost + float(share @ costs[row])
        out["turnover"][row] += float(share @ turnover[row])
        grown = share * (1.0 + returns[row])
        if grown.sum() > 0.0:
            share = grown / grown.sum()
        out["exposure"][row] = float(share @ exposure[row])
        for m, (member, first) in enumerate(zip(members, firsts)):
            held = member.weights[first + row]
            for a in np.flatnonzero(held).tolist():
                weights[row, place[member.assets[a]]] += share[m] * held[a]
                if carry[m]:
                    weights[row, place[f"{member.assets[a]}{PERP}"]] += share[m] * held[a]
        equity *= (1.0 + day) * kept
        charged += cost
    info = {
        "members": [member.name for member in members],
        "carry": False,
        "rebalance_costs": charged,
        "open_exit_cost": float(sum(s * (m.info.get("open_exit_cost") or 0.0) for s, m in zip(share, members))),
    }
    return Result(name, ts, out["returns"], out["gross_returns"], out["turnover"], out["exposure"], out["costs"],
                  weights, tuple(assets), info)
