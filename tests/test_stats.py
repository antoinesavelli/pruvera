"""Tests for the statistics helpers: known values, edge cases, reproducibility."""

from __future__ import annotations

import math

from bench import stats


def test_wilson_matches_known_values_and_handles_the_edges() -> None:
    lo, hi = stats.wilson(8, 10)
    assert round(lo, 3) == 0.49 and round(hi, 3) == 0.943
    assert stats.wilson(0, 0) == (0.0, 1.0)
    assert stats.wilson(0, 10)[0] == 0.0 and abs(stats.wilson(10, 10)[1] - 1.0) < 1e-9
    wide, narrow = stats.wilson(5, 10), stats.wilson(50, 100)
    assert wide[1] - wide[0] > narrow[1] - narrow[0]


def test_pass_hat_k_is_the_chance_that_k_tries_all_succeed() -> None:
    outcomes = {"a": [True, True, True, True], "b": [True, False, False, False]}
    assert stats.pass_hat_k(outcomes, 1) == (1.0 + 0.25) / 2
    assert stats.pass_hat_k(outcomes, 2) == (1.0 + 0.0) / 2
    assert math.isnan(stats.pass_hat_k({"a": [True]}, 2)), "too few trials to say anything"
    assert stats.pass_hat_k({"a": [True, False, True]}, 2) == math.comb(2, 2) / math.comb(3, 2)


def test_cluster_rate_counts_each_task_once() -> None:
    assert stats.cluster_rate({"a": [True] * 9, "b": [False]}) == 0.5


def test_bootstrap_diff_is_reproducible_and_brackets_a_clear_difference() -> None:
    a = {f"t{i}": [False, False, False] for i in range(8)}
    b = {f"t{i}": [True, True, False] for i in range(8)}
    first = stats.bootstrap_diff(a, b, draws=500, seed=3)
    assert first == stats.bootstrap_diff(a, b, draws=500, seed=3)
    point, lo, hi = first
    assert round(point, 3) == 0.667 and 0 < lo <= point <= hi <= 1
    same = stats.bootstrap_diff(a, a, draws=500)
    assert same[0] == 0 and same[1] <= 0 <= same[2], "no true difference: the interval spans 0"
    assert all(math.isnan(x) for x in stats.bootstrap_diff({"x": [True]}, {"y": [True]}))


def test_min_detectable_effect_shrinks_with_more_trials() -> None:
    assert stats.min_detectable_effect(12, 3) > stats.min_detectable_effect(12, 12)
    assert 0.3 < stats.min_detectable_effect(12, 3) < 0.4, "12 questions x 3 cannot see +0.10"
