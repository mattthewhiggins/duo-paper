"""Section 10's corrections for testing many variants: deflated Sharpe, clusters, CSCV and a bootstrap interval.

Only numpy and the standard library, so these can be checked without the rest of the harness. Every Sharpe here is
daily, not annualised: divide summarise's sharpe by sqrt(365) before passing it to dsr_raw. Where the rules leave a
choice open, these are the readings taken:

- moments returns a dict. Skew and kurtosis are central moments over T days (ddof=0), the usual sample figures.
  std(ddof=1) is only for sr. With no spread they are 0/0, so they're NaN, and the DSR of a flat series is NaN.
- A NaN flows through: a NaN in returns or trial_srs, or one trial, whose variance doesn't exist, gives a NaN DSR. The
  gate reads a NaN as luck not ruled out. A series with no spread at all has a Sharpe of 0.0, as in metrics.py, and
  that includes one whose spread is only rounding.
- cluster_variants' members include the representative. A variant whose correlation can't be worked out, such as a flat
  one, joins nothing, so two flat variants are two clusters. A variant with a NaN Sharpe (under 2 days) is visited
  last. As a representative it makes dsr_eff NaN.
- The rule doesn't say what a flat variant is to dsr_eff, and it matters. Its Sharpe of exactly 0 can narrow the spread
  of Sharpes faster than it raises n, so a variant that never trades would make the best one look less lucky, and in a
  field of high Sharpes it can do the opposite. No one reading is the strict one in every field, so dsr_eff works it out
  with them each alone, as one cluster and left out, and the lowest stands. It can only add a luck note, never take one
  away, and it changes nothing where no column is flat.
- Sharpes within 1e-12 count as tied, and a correlation within 1e-12 of the threshold as at it. Those gaps are rounding:
  a column and 3 times it have the same Sharpe on paper, but not always in floating point, and correlations of exactly
  0.7 on paper can come out as 0.6999999999999998.
- dsr_raw's default of 3,738 is the specification's. Amendment 3 also counts state.rule_tests from data/paper.db when
  the gate runs, so the caller must pass prior_trials=3738 + rule_tests. This file can't read that database.
  trial_srs must hold each variant's track R run as well, under Amendment 4.
"""
from __future__ import annotations

import itertools
import math
from statistics import NormalDist

import numpy as np

EULER_GAMMA = 0.5772156649015329
PRIOR_TRIALS = 3738  # The rule runs made before the preregistration, from its section 2.
_NORMAL = NormalDist()
_ROUNDING = 1e-7  # A std under this share of the series' own size can't be told apart from rounding.
_TIE = 1e-12


def _series(returns) -> np.ndarray:
    values = np.asarray(returns, dtype=np.float64)
    if values.ndim != 1:
        raise ValueError(f"expected one series, got shape {values.shape}")
    return values


def _is_flat(values: np.ndarray) -> bool:
    # numpy gives identical returns a std near 1e-19 rather than 0, and dividing by that gives an absurd Sharpe.
    return float(values.std(ddof=1)) <= _ROUNDING * math.sqrt(np.mean(values * values))


def _daily_sharpe(values: np.ndarray) -> float:
    """mean / std(ddof=1). 0.0 if std is 0, and NaN under 2 days, which have no std."""
    if values.size < 2:
        return math.nan
    if _is_flat(values):
        return 0.0
    return float(values.mean() / values.std(ddof=1))


def _sample_variance(values: np.ndarray) -> float:
    return float(values.var(ddof=1)) if values.size >= 2 else math.nan


def moments(returns) -> dict:
    """sr (daily, mean / std ddof=1), skew, kurtosis (not excess, normal is 3) and T."""
    values = _series(returns)
    sr = _daily_sharpe(values)
    if values.size < 2 or _is_flat(values):
        return {"sr": sr, "skew": math.nan, "kurtosis": math.nan, "T": int(values.size)}
    deviations = values - values.mean()
    variance = np.mean(deviations**2)
    return {
        "sr": sr,
        "skew": float(np.mean(deviations**3) / variance**1.5),
        "kurtosis": float(np.mean(deviations**4) / variance**2),
        "T": int(values.size),
    }


def psr(sr, T, skew, kurtosis, sr_star=0.0) -> float:
    """Phi(((sr - sr_star) * sqrt(T - 1)) / sqrt(1 - skew * sr + (kurtosis - 1) / 4 * sr^2)). Everything is daily."""
    spread = 1 - skew * sr + (kurtosis - 1) / 4 * sr**2
    # Moments of a real series can't make spread negative, but figures passed by hand can. NaN fails every test.
    if T < 1 or not spread > 0:
        return math.nan
    return _NORMAL.cdf((sr - sr_star) * math.sqrt(T - 1) / math.sqrt(spread))


def expected_max_sr(n, var_sr) -> float:
    """The highest Sharpe to expect from n trials with no skill, in sqrt(var_sr)'s units. 0 for n = 1 or var_sr = 0."""
    if not n >= 1:
        raise ValueError(f"n must be at least 1, got {n}")
    if n == 1:
        return 0.0
    if math.isnan(var_sr):
        return math.nan
    if var_sr == 0:
        return 0.0
    z = _NORMAL.inv_cdf
    return math.sqrt(var_sr) * ((1 - EULER_GAMMA) * z(1 - 1 / n) + EULER_GAMMA * z(1 - 1 / (n * math.e)))


def dsr(returns, n, var_sr) -> float:
    """psr of the returns, measured against the best Sharpe n skill-free trials would be expected to show."""
    figures = moments(returns)
    return psr(figures["sr"], figures["T"], figures["skew"], figures["kurtosis"], expected_max_sr(n, var_sr))


def _correlation(a: np.ndarray, b: np.ndarray, min_overlap: int) -> float:
    """Pearson correlation over the days both have. NaN under min_overlap of them, or if either has no spread there."""
    shared = ~np.isnan(a) & ~np.isnan(b)
    x, y = a[shared], b[shared]
    if x.size < max(min_overlap, 2) or _is_flat(x) or _is_flat(y):
        return math.nan
    x, y = x - x.mean(), y - y.mean()
    # np.sum, not a dot product: BLAS may round differently with memory alignment, and outputs must be byte-identical.
    return float(np.sum(x * y) / math.sqrt(np.sum(x * x) * np.sum(y * y)))


def _checked_variants(names, matrix) -> tuple[list, np.ndarray]:
    names, matrix = list(names), np.asarray(matrix, dtype=np.float64)
    if matrix.ndim != 2 or matrix.shape[1] != len(names):
        raise ValueError(f"matrix has shape {matrix.shape} for {len(names)} names")
    if len(set(names)) != len(names):
        raise ValueError("names must be unique")
    return names, matrix


def _column_sharpes(matrix: np.ndarray) -> list[float]:
    return [_daily_sharpe(column[~np.isnan(column)]) for column in matrix.T]


def cluster_variants(names, matrix, threshold=0.7, min_overlap=60) -> list:
    """Greedy clusters of a D x N matrix of daily returns, NaN where a variant has no return.

    Variants are visited by descending daily Sharpe, ties by name. Each joins the first cluster whose representative has
    a correlation at or above threshold over their shared days, or starts a new one as its representative. Returns a
    list of (representative, members).
    """
    names, matrix = _checked_variants(names, matrix)
    sharpes = _column_sharpes(matrix)

    def visit_order(column):
        sharpe = sharpes[column]
        return (math.isnan(sharpe), 0.0 if math.isnan(sharpe) else -round(sharpe, 12), names[column])

    clusters = []
    for column in sorted(range(len(names)), key=visit_order):
        for representative, members in clusters:
            # A NaN correlation compares false, so under min_overlap counts as uncorrelated.
            if _correlation(matrix[:, column], matrix[:, representative], min_overlap) >= threshold - _TIE:
                members.append(names[column])
                break
        else:
            clusters.append((column, [names[column]]))
    return [(names[representative], members) for representative, members in clusters]


def dsr_raw(returns, trial_srs, prior_trials=PRIOR_TRIALS) -> float:
    """DSR with n = every trial and var_sr = their sample variance. trial_srs are daily. See the top on prior_trials."""
    trial_srs = _series(trial_srs)
    return dsr(returns, trial_srs.size + prior_trials, _sample_variance(trial_srs))


def _never_moves(column: np.ndarray) -> bool:
    values = column[~np.isnan(column)]
    return values.size >= 2 and _is_flat(values)


def dsr_eff(returns, names, matrix) -> float:
    """DSR with n = the cluster count and var_sr = the sample variance of the representatives' daily Sharpes.

    With flat columns, the lowest of three readings of them. See the top of this file.

    It doesn't deflate for choosing inside a cluster: with one cluster it's plain PSR(0), however many variants the
    winner was chosen from. So the report passes every family on both tracks here, with DSR_raw beside it (Amendment 6,
    item 7).
    """
    names, matrix = _checked_variants(names, matrix)
    sharpe_of = dict(zip(names, _column_sharpes(matrix)))
    representatives = [representative for representative, _ in cluster_variants(names, matrix)]
    flat = [name for name, column in zip(names, matrix.T) if _never_moves(column)]
    moving = [name for name in representatives if name not in flat]
    readings = [representatives]
    if flat:
        readings.append([*moving, flat[0]])
        if moving:
            readings.append(moving)
    figures = [
        dsr(returns, len(chosen), _sample_variance(np.array([sharpe_of[name] for name in chosen])))
        for chosen in readings
    ]
    return float(np.min(figures))  # np.min passes a NaN on, where min() would give an answer that depends on the order.


def _half_sharpes(sums: np.ndarray, squares: np.ndarray, blocks: list[int], rows: int) -> np.ndarray:
    """Each column's daily Sharpe over the given blocks, from their stored sums, with 0.0 where it has no spread."""
    total, square = sums[blocks].sum(axis=0), squares[blocks].sum(axis=0)
    spread = square - total * total / rows  # This is (rows - 1) x the variance.
    has_spread = spread > _ROUNDING**2 * square  # The same limit as _is_flat, on the squares.
    out = np.zeros(total.shape)
    out[has_spread] = total[has_spread] / rows / np.sqrt(spread[has_spread] / (rows - 1))
    return out


def pbo_cscv(matrix, partitions=16) -> dict:
    """Probability of backtest overfitting by CSCV, for a D x N matrix of daily returns with no NaN.

    Rows are split into consecutive equal blocks, dropping the first D mod partitions rows. For every choice of half the
    blocks as in-sample, the in-sample best by Sharpe (ties to the lowest column) gets its out-of-sample rank, best = N,
    average ranks for ties. PBO is the share of choices where it lands in the bottom half: logit <= 0.

    Returns {"pbo", "logits" (one per choice, in itertools.combinations order), "combos"}.
    """
    matrix = np.asarray(matrix, dtype=np.float64)
    if matrix.ndim != 2 or matrix.shape[1] < 1:
        raise ValueError(f"matrix must be D x N, got shape {matrix.shape}")
    if not np.isfinite(matrix).all():
        raise ValueError("matrix must have no NaN or infinite values")
    if partitions < 2 or partitions % 2:
        raise ValueError(f"partitions must be even, so that half can be chosen, got {partitions}")
    rows_per_block = matrix.shape[0] // partitions
    half_rows = rows_per_block * (partitions // 2)
    if half_rows < 2:
        raise ValueError(f"{matrix.shape[0]} rows in {partitions} partitions leave under 2 rows in each half")
    columns = matrix.shape[1]
    blocks = matrix[matrix.shape[0] % partitions :].reshape(partitions, rows_per_block, columns)
    # Stored per block, so each choice costs a few sums rather than a pass over every row. The out-of-sample half is
    # summed too, not taken from the total: subtraction would leave rounding where a variant was flat for whole blocks.
    sums, squares = blocks.sum(axis=1), (blocks * blocks).sum(axis=1)

    logits = []
    for in_sample in itertools.combinations(range(partitions), partitions // 2):
        out_sample = [block for block in range(partitions) if block not in in_sample]
        in_sharpes = _half_sharpes(sums, squares, list(in_sample), half_rows)
        out_sharpes = _half_sharpes(sums, squares, out_sample, half_rows)
        best = int(np.argmax(in_sharpes >= in_sharpes.max() - _TIE))  # argmax takes the first True: the lowest column.
        score = out_sharpes[best]
        below = np.count_nonzero(out_sharpes < score - _TIE)
        tied = np.count_nonzero(np.abs(out_sharpes - score) <= _TIE)  # This counts the best column itself.
        rank = below + (tied + 1) / 2
        w = rank / (columns + 1)
        logits.append(math.log(w / (1 - w)))
    logits = np.array(logits)
    return {"pbo": float(np.mean(logits <= 0)), "logits": logits, "combos": int(logits.size)}


def block_bootstrap_ci(returns, statistic, block=20, n=2000, seed=0, level=0.95) -> tuple[float, float]:
    """A percentile interval for statistic(returns) from n resamples of circular blocks, deterministic for a seed.

    Blocks keep the runs of good and bad days that a day-by-day resample would break up, or the interval would be too
    narrow.
    Each resample is as long as the returns, and a block running off the end carries on from the start.
    """
    values = _series(returns)
    if not values.size or block < 1 or n < 1 or not 0 < level < 1:
        raise ValueError(f"bad bootstrap settings: {values.size} days, block={block}, n={n}, level={level}")
    rng = np.random.default_rng(seed)
    blocks_needed = math.ceil(values.size / block)
    offsets = np.arange(block)
    results = np.empty(n)
    for run in range(n):
        starts = rng.integers(0, values.size, size=blocks_needed)
        rows = ((starts[:, None] + offsets) % values.size).ravel()[: values.size]
        results[run] = statistic(values[rows])
    # np.quantile passes a NaN on, so a statistic that sometimes fails can't produce a tidy-looking interval.
    low, high = np.quantile(results, [(1 - level) / 2, 1 - (1 - level) / 2])
    return float(low), float(high)
