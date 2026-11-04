import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import NormalDist

LEVEL = 0.95
SEED = 0
RANDOMIZATION_RESAMPLES = 2000
ARM_BOOTSTRAP_RESAMPLES = 4000
SLICE_MEAN_RESAMPLES = 4000


def randomization_p(
    baseline: Sequence[float],
    candidate: Sequence[float],
    resamples: int = RANDOMIZATION_RESAMPLES,
    seed: int = SEED,
) -> float:
    """Two-sided p-value of a paired randomization test over the per-datapoint differences."""
    if len(baseline) != len(candidate) or not baseline:
        raise ValueError("randomization_p needs two non-empty sequences of the same length")
    diffs = [c - b for b, c in zip(baseline, candidate, strict=True)]
    observed = abs(sum(diffs))
    rng = random.Random(seed)
    hits = sum(
        abs(sum(d if rng.random() < 0.5 else -d for d in diffs)) >= observed - 1e-12
        for _ in range(resamples)
    )
    return (hits + 1) / (resamples + 1)


def holm(p_values: Sequence[float]) -> list[float]:
    """Holm-adjusted p-values for one declared family, in the order given."""
    m = len(p_values)
    adjusted = [0.0] * m
    running = 0.0
    for rank, index in enumerate(sorted(range(m), key=lambda i: p_values[i])):
        running = max(running, min(1.0, (m - rank) * p_values[index]))
        adjusted[index] = running
    return adjusted


@dataclass(frozen=True)
class Interval:
    low: float
    high: float
    method: str


def _percentiles(means: list[float], level: float) -> tuple[float, float]:
    means.sort()
    low = round((1 - level) / 2 * len(means))
    return means[low], means[min(len(means) - low, len(means) - 1)]


def wilson(successes: int, n: int, level: float = LEVEL) -> tuple[float, float]:
    """Wilson score interval for a proportion."""
    if n < 1 or not 0 <= successes <= n:
        raise ValueError("wilson needs 0 <= successes <= n and n >= 1")
    z = NormalDist().inv_cdf(1 - (1 - level) / 2)
    share = successes / n
    centre = (share + z**2 / (2 * n)) / (1 + z**2 / n)
    half = z / (1 + z**2 / n) * math.sqrt(share * (1 - share) / n + z**2 / (4 * n**2))
    return max(0.0, centre - half), min(1.0, centre + half)


def arm_interval(
    values: Sequence[float],
    resamples: int = ARM_BOOTSTRAP_RESAMPLES,
    seed: int = SEED,
    level: float = LEVEL,
) -> Interval:
    """Interval on the mean of per-datapoint values: Wilson for 0/1 values, else a seeded bootstrap."""
    if not values:
        raise ValueError("arm_interval needs at least one value")
    if all(v in (0.0, 1.0) for v in values):
        low, high = wilson(sum(1 for v in values if v == 1.0), len(values), level)
        return Interval(low, high, "wilson")
    n = len(values)
    rng = random.Random(seed)
    means = [sum(rng.choice(values) for _ in range(n)) / n for _ in range(resamples)]
    return Interval(*_percentiles(means, level), "percentile_bootstrap")


def slice_mean(groups: Mapping[str, Sequence[float]]) -> float:
    """Equal-weight mean of the per-slice-value means."""
    if not groups or not all(groups.values()):
        raise ValueError("slice_mean needs at least one slice value, each with values")
    return sum(sum(values) / len(values) for values in groups.values()) / len(groups)


def slice_mean_interval(
    groups: Mapping[str, Sequence[float]],
    resamples: int = SLICE_MEAN_RESAMPLES,
    seed: int = SEED,
    level: float = LEVEL,
) -> Interval:
    """Stratified bootstrap interval on slice_mean: rows resampled within each slice value."""
    slice_mean(groups)
    rng = random.Random(seed)
    strata = [list(groups[key]) for key in sorted(groups)]
    means = [
        sum(sum(rng.choice(rows) for _ in rows) / len(rows) for rows in strata) / len(strata)
        for _ in range(resamples)
    ]
    return Interval(*_percentiles(means, level), "stratified_bootstrap")


def unmeasured(failed: int, cells: int, bar: float) -> str | None:
    """Why a cell is unmeasured when its failed share of planned cells is over the bar."""
    if cells < 1 or not 0 <= failed <= cells:
        raise ValueError("unmeasured needs 0 <= failed <= cells and cells >= 1")
    if failed / cells <= bar:
        return None
    return f"{failed / cells:.0%} failed, over the {bar:.0%} dead-run bar"
