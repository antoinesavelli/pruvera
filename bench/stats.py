"""Small, dependency-free statistics for trial results: intervals, pass^k, clustered comparisons.

Trials repeat over a small set of tasks, so the trials of one task are not independent. Every
comparison here resamples tasks (clusters), never single trials, with each task's own rate drawn
from its Beta posterior so a task that always passes still carries uncertainty, and every claim
carries its interval. Randomness uses a seeded local generator, so a report is reproducible.
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
    """pass^k: mean over tasks of the chance that k independent tries all succeed."""
    # Per task the unbiased estimate is C(c, k) / C(n, k) for c successes in n >= k trials; tasks
    # with fewer than k trials are skipped. pass^1 is the plain pass rate.
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
    alpha: float = 0.05,
) -> tuple[float, float, float]:
    """(difference b - a in cluster rate, lower, upper (1 - alpha)) resampling the shared tasks."""
    # Tasks are resampled, and each task's rate is drawn from a Beta(k + 1/2, n - k + 1/2)
    # posterior (Jeffreys) instead of resampling its few trials: a task solved 4 of 4 times in
    # both arms would otherwise have zero variance and an interval of exactly [0, 0].
    tasks = sorted(set(a) & set(b))
    if not tasks:
        return float("nan"), float("nan"), float("nan")
    rng = random.Random(seed)

    def rate(results: Sequence[bool]) -> float:
        k, n = sum(results), len(results)
        return rng.betavariate(k + 0.5, n - k + 0.5)

    diffs = []
    for _ in range(draws):
        picked = [tasks[rng.randrange(len(tasks))] for _ in tasks]
        diffs.append(
            sum(rate(b[t]) for t in picked) / len(picked)
            - sum(rate(a[t]) for t in picked) / len(picked)
        )
    diffs.sort()
    point = cluster_rate({t: b[t] for t in tasks}) - cluster_rate({t: a[t] for t in tasks})
    return point, diffs[int(alpha / 2 * draws)], diffs[int((1 - alpha / 2) * draws) - 1]


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


def newcombe_upper(k_base: int, n_base: int, k_cand: int, n_cand: int) -> float:
    """Upper 95% bound of (candidate rate - baseline rate), Newcombe's hybrid-score interval."""
    if n_base == 0 or n_cand == 0:
        return 1.0
    p_b, p_c = k_base / n_base, k_cand / n_cand
    _, upper_c = wilson(k_cand, n_cand)
    lower_b, _ = wilson(k_base, n_base)
    return float(p_c - p_b + math.sqrt((upper_c - p_c) ** 2 + (p_b - lower_b) ** 2))


def fisher_greater(k_base: int, n_base: int, k_cand: int, n_cand: int) -> float:
    """One-sided Fisher exact p-value that the candidate's event rate exceeds the baseline's."""
    # P(X >= k_cand) for X hypergeometric with the pooled event count fixed; 1.0 with no events.
    events, total = k_base + k_cand, n_base + n_cand
    if events == 0 or n_base == 0 or n_cand == 0:
        return 1.0
    denom = math.comb(total, events)
    return (
        sum(
            math.comb(n_cand, x) * math.comb(n_base, events - x)
            for x in range(k_cand, min(n_cand, events) + 1)
        )
        / denom
    )


def sign_test(higher: int, lower: int) -> float:
    """Two-sided exact sign test p-value for `higher` tasks above, `lower` below (ties dropped)."""
    n = higher + lower
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, k) for k in range(min(higher, lower) + 1)) / 2**n
    return float(min(1.0, 2 * tail))


def cluster_ci(
    outcomes: Mapping[str, Sequence[bool]], *, draws: int = 4000, seed: int = 1
) -> tuple[float, float, float]:
    """(mean of per-task success rates, lower, upper 95%), resampling tasks, then trials."""
    tasks = sorted(t for t, r in outcomes.items() if r)
    if not tasks:
        return float("nan"), float("nan"), float("nan")
    rng = random.Random(seed)
    means = []
    for _ in range(draws):
        picked = [tasks[rng.randrange(len(tasks))] for _ in tasks]
        total = 0.0
        for t in picked:
            r = outcomes[t]
            total += sum(r[rng.randrange(len(r))] for _ in r) / len(r)
        means.append(total / len(picked))
    means.sort()
    return cluster_rate(outcomes), means[int(0.025 * draws)], means[int(0.975 * draws) - 1]


def min_detectable_effect(n_tasks: int, trials_per_task: int, p: float = 0.5) -> float:
    """Rough smallest true difference in rates detectable at 80% power, two-sided 5%."""
    # About 2.8 standard errors of a difference, at the worst-case binary spread; the design effect
    # from clustering is ignored, so treat it as optimistic.
    n = max(1, n_tasks * trials_per_task)
    return 2.8 * math.sqrt(2 * p * (1 - p) / n)
