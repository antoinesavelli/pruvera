"""Small, dependency-free statistics for trial results: intervals, pass^k, clustered comparisons.

Trials repeat over a small set of tasks, so the trials of one task are not independent. Every
comparison here resamples tasks (clusters), never single trials, and every claim carries its
interval. Randomness uses a seeded local generator, so a report is reproducible.
Depends on: the standard library.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence

Z95 = 1.959964


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for k successes in n trials; (0, 1) when there are no trials."""
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def pass_hat_k(outcomes: Mapping[str, Sequence[bool]], k: int) -> float:
    """pass^k: mean over tasks of the chance that k independent tries all succeed.

    Per task the unbiased estimate is C(c, k) / C(n, k) for c successes in n >= k trials; tasks
    with fewer than k trials are skipped. Reliability, not luck: pass^1 is the plain pass rate.
    """
    scores = []
    for results in outcomes.values():
        n, c = len(results), sum(results)
        if n >= k:
            scores.append(math.comb(c, k) / math.comb(n, k))
    return sum(scores) / len(scores) if scores else float("nan")


def cluster_rate(outcomes: Mapping[str, Sequence[bool]]) -> float:
    """Mean of per-task success rates (each task counts once, however many trials it has)."""
    rates = [sum(r) / len(r) for r in outcomes.values() if r]
    return sum(rates) / len(rates) if rates else float("nan")


def bootstrap_diff(
    a: Mapping[str, Sequence[bool]],
    b: Mapping[str, Sequence[bool]],
    *,
    draws: int = 4000,
    seed: int = 1,
) -> tuple[float, float, float]:
    """(difference b - a in cluster rate, lower, upper 95%) resampling the shared tasks.

    Trials within a task are resampled too, so both sources of noise are in the interval.
    """
    tasks = sorted(set(a) & set(b))
    if not tasks:
        return float("nan"), float("nan"), float("nan")
    rng = random.Random(seed)

    def rate(results: Sequence[bool]) -> float:
        drawn = [results[rng.randrange(len(results))] for _ in results]
        return sum(drawn) / len(drawn)

    diffs = []
    for _ in range(draws):
        picked = [tasks[rng.randrange(len(tasks))] for _ in tasks]
        diffs.append(
            sum(rate(b[t]) for t in picked) / len(picked)
            - sum(rate(a[t]) for t in picked) / len(picked)
        )
    diffs.sort()
    point = cluster_rate({t: b[t] for t in tasks}) - cluster_rate({t: a[t] for t in tasks})
    return point, diffs[int(0.025 * draws)], diffs[int(0.975 * draws) - 1]


def bootstrap_ratio(
    num: Mapping[str, Sequence[float]],
    den: Mapping[str, Sequence[float]],
    *,
    draws: int = 4000,
    seed: int = 1,
) -> tuple[float, float, float]:
    """(ratio of mean task means num/den, lower, upper 95%), resampling shared tasks and trials."""
    tasks = sorted(t for t in set(num) & set(den) if num[t] and den[t])
    if not tasks:
        return float("nan"), float("nan"), float("nan")
    rng = random.Random(seed)

    def mean(values: Sequence[float]) -> float:
        drawn = [values[rng.randrange(len(values))] for _ in values]
        return sum(drawn) / len(drawn)

    def ratio(picked: list[str], resample: bool) -> float:
        def m(vals: Sequence[float]) -> float:
            return mean(vals) if resample else sum(vals) / len(vals)

        top = sum(m(num[t]) for t in picked)
        bottom = sum(m(den[t]) for t in picked)
        return top / bottom if bottom else float("nan")

    draws_list = sorted(
        r
        for r in (
            ratio([tasks[rng.randrange(len(tasks))] for _ in tasks], True) for _ in range(draws)
        )
        if not math.isnan(r)
    )
    if not draws_list:
        return float("nan"), float("nan"), float("nan")
    low, high = (
        draws_list[int(0.025 * len(draws_list))],
        draws_list[int(0.975 * len(draws_list)) - 1],
    )
    return ratio(tasks, False), low, high


def min_detectable_effect(n_tasks: int, trials_per_task: int, p: float = 0.5) -> float:
    """Rough smallest true difference in rates detectable at 80% power, two-sided 5%.

    Uses the between-task spread of a binary outcome as the worst case (design effect from
    clustering is ignored, so treat it as optimistic): about 2.8 standard errors of a difference.
    """
    n = max(1, n_tasks * trials_per_task)
    return 2.8 * math.sqrt(2 * p * (1 - p) / n)
