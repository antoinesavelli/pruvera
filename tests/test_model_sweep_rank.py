"""The model-sweep ranking follows plans/MODEL_SWEEP.md decision 5, and decision 4's reuse rules.

Depends on: spikes/model_sweep_rank.py, pytest.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
N = 8  # issues in the synthetic kind: floor 6, a study stops at its 3rd failure


@pytest.fixture(scope="module")
def sweep() -> ModuleType:
    """Loads the rank spike as a module."""
    path = ROOT / "spikes" / "model_sweep_rank.py"
    spec = importlib.util.spec_from_file_location("spike_model_sweep_rank", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def trial(
    sweep: ModuleType, issue: int, ok: bool, repeat: int = 1, secs: float = 60.0, **kw: Any
) -> Any:
    """One synthetic trial."""
    return sweep.Trial(
        f"i{issue}", repeat, ok, kw.pop("fault", False), kw.pop("missing", False), secs, **kw
    )


def study(
    sweep: ModuleType,
    model: str,
    k: int,
    gib: float = 10.0,
    secs: float = 60.0,
    n: int = N,
    **kw: Any,
) -> Any:
    """A complete one-repeat study with k successes in n issues."""
    trials = tuple(trial(sweep, i, i < k, secs=secs) for i in range(n))
    return sweep.Study(model, gib, trials, **kw)


def names(rows: Any) -> list[str]:
    """Model names of rows."""
    return [r.model for r in rows]


def test_floor_and_stop_limit_follow_the_plan(sweep: ModuleType) -> None:
    """ceil(0.75 N): 6 of 8, 8 of 10, 7 of 9; the stop limit is N - floor + 1 (3 at N = 8 or 10)."""
    assert [sweep.floor_for(n) for n in (7, 8, 9, 10, 12)] == [6, 6, 7, 8, 9]
    assert [sweep.max_failures(n) for n in (7, 8, 9, 10, 12)] == [2, 3, 3, 3, 4]


def test_higher_k_ranks_first(sweep: ModuleType) -> None:
    """Rank is by k descending before anything else, however big or slow the model."""
    result = sweep.rank(
        [study(sweep, "small", 5, gib=5.0), study(sweep, "big", 8, gib=19.0, secs=900.0)], N
    )
    assert names(result.ranked) == ["big", "small"]


def test_ties_go_to_the_smaller_model_on_disk(sweep: ModuleType) -> None:
    """Equal k: the smaller disk size ranks higher, whatever the speed or the name."""
    result = sweep.rank(
        [
            study(sweep, "a-big", 7, gib=18.9, secs=10.0),
            study(sweep, "z-small", 7, gib=5.0, secs=500.0),
        ],
        N,
    )
    assert names(result.ranked) == ["z-small", "a-big"]


def test_equal_size_goes_to_the_lower_median_seconds_then_the_name(sweep: ModuleType) -> None:
    """Same k and the same size to two decimals: lower median seconds, then the name."""
    result = sweep.rank(
        [
            study(sweep, "slow", 7, gib=13.351, secs=90.0),
            study(sweep, "fast", 7, gib=13.349, secs=40.0),
            study(sweep, "b", 7, gib=14.0, secs=40.0),
            study(sweep, "a", 7, gib=14.0, secs=40.0),
        ],
        N,
    )
    assert names(result.ranked) == ["fast", "slow", "a", "b"]


def test_the_floor_drops_a_top_3_model_and_nothing_pads_the_list(sweep: ModuleType) -> None:
    """A top-3 model below ceil(0.75 N) does not go forward; fewer than three is a fine result."""
    result = sweep.rank(
        [study(sweep, "m8", 8), study(sweep, "m6", 6, gib=11.0), study(sweep, "m5", 5, gib=12.0),
         study(sweep, "m4", 4, gib=13.0)],
        N,
    )  # fmt: skip
    assert names(result.top3) == ["m8", "m6", "m5"]
    assert names(result.forward) == ["m8", "m6"]
    assert names(result.dropped) == ["m5"]
    assert "m4" not in names(result.forward), "the fourth place does not take the dropped place"
    text = sweep.markdown("specified-edit", result)
    assert "nothing pads the list" in text and "Dropped by the floor" in text


def test_fewer_than_three_studies_gives_fewer_than_three(sweep: ModuleType) -> None:
    """With two complete studies the top 3 is two models and the band is empty."""
    result = sweep.rank([study(sweep, "x", 8), study(sweep, "y", 7)], N)
    assert names(result.top3) == ["x", "y"] and result.band == ()


def test_the_lowest_repeat_of_a_screened_model_is_used_and_the_others_are_not(
    sweep: ModuleType,
) -> None:
    """A screen's repeat 1 is its n = 1 row; repeats 2 and 3 add nothing to k (the n = 3 cell is
    only printed)."""
    trials = []
    for i in range(N):
        trials += [
            trial(sweep, i, i < 4, repeat=1),  # repeat 1: 4 of 8
            trial(sweep, i, True, repeat=2),  # later repeats would make it 8 of 8
            trial(sweep, i, True, repeat=3),
        ]
    screened = sweep.Study("screened", 14.1, tuple(trials))
    row = sweep.summarise(screened, N)
    assert (row.k, row.scored, row.reused) == (4, 8, True)
    assert row.cell == (20, 24)
    shuffled = sweep.Study("screened", 14.1, tuple(reversed(trials)))
    assert sweep.summarise(shuffled, N).k == 4, "file order does not matter, the repeat number does"


def test_a_scorable_later_repeat_stands_in_for_a_faulted_first_one(sweep: ModuleType) -> None:
    """The lowest-numbered repeat WITH a scorable row: a faulted repeat 1 is skipped."""
    trials = [trial(sweep, 0, False, repeat=1, fault=True), trial(sweep, 0, True, repeat=2)]
    picked = sweep.pick(trials)
    assert picked["i0"].repeat == 2 and picked["i0"].primary


def test_an_infrastructure_fault_is_counted_apart_and_never_scored(sweep: ModuleType) -> None:
    """A fault is not a failure and not a success: k, the scored count and the median ignore it."""
    trials = tuple(trial(sweep, i, True, secs=30.0) for i in range(7))
    trials += (trial(sweep, 7, False, fault=True, secs=600.0),)
    row = sweep.summarise(sweep.Study("m", 10.0, trials), N)
    assert (row.k, row.scored, row.failures, row.faults) == (7, 7, 0, 1)
    assert row.status == "partial", "the faulted issue has no scorable trial yet"
    assert row.median_secs == 30.0
    rerun = sweep.summarise(
        sweep.Study("m", 10.0, (*trials, trial(sweep, 7, True, repeat=1, secs=40.0))), N
    )
    assert (rerun.status, rerun.k, rerun.faults) == ("complete", 8, 1)


def test_missing_answers_stay_in_n_and_are_counted_apart(sweep: ModuleType) -> None:
    """A missing answer is a scored trial (a failure here) and is also counted."""
    trials = tuple(trial(sweep, i, i != 0, missing=i == 0) for i in range(N))
    row = sweep.summarise(sweep.Study("m", 10.0, trials), N)
    assert (row.k, row.scored, row.missing) == (7, N, 1)


def test_a_stopped_study_has_no_rank_and_prints_at_most(sweep: ModuleType) -> None:
    """Three failures at N = 8: below the floor for good, no k, no rank; "at most N - F" in the
    table."""
    stopped = sweep.Study(
        "weak",
        5.0,
        (
            trial(sweep, 0, False),
            trial(sweep, 1, True),
            trial(sweep, 2, False),
            trial(sweep, 3, False),
        ),
    )
    result = sweep.rank([study(sweep, "ok", 7), stopped], N)
    assert names(result.ranked) == ["ok"] and names(result.stopped) == ["weak"]
    text = sweep.markdown("specified-edit", result)
    assert "stopped after 3 failures in 4 trials, below the floor; at most 5" in text


def test_a_partial_study_is_not_called_stopped(sweep: ModuleType) -> None:
    """Two failures at N = 8 can still reach the floor: the study is unfinished, not stopped."""
    partial = sweep.Study("p", 5.0, (trial(sweep, 0, False), trial(sweep, 1, False)))
    assert sweep.summarise(partial, N).status == "partial"


def test_the_boundary_band_is_within_one_trial_of_the_third(sweep: ModuleType) -> None:
    """Models with k within one of the third-ranked k are listed; others are not."""
    result = sweep.rank(
        [
            study(sweep, "a", 8, gib=1.0),
            study(sweep, "b", 7, gib=2.0),
            study(sweep, "c", 6, gib=3.0),
            study(sweep, "d", 5, gib=4.0),
            study(sweep, "e", 4, gib=5.0),
        ],
        N,
    )  # fmt: skip
    assert names(result.band) == ["b", "c", "d"]
    assert names(result.forward) == ["a", "b", "c"]


def test_a_stopped_study_that_could_be_in_the_band_is_listed_apart(sweep: ModuleType) -> None:
    """A stopped model whose ceiling reaches the band is mentioned, not ranked."""
    stopped = sweep.Study(
        "weak", 5.0, tuple(trial(sweep, i, i == 0) for i in range(3))
    )  # 1 of 3, ceiling 5
    result = sweep.rank(
        [study(sweep, "a", 8), study(sweep, "b", 7), study(sweep, "c", 6), stopped], N
    )
    assert names(result.possible) == ["weak"] and "weak" not in names(result.ranked)


def test_screen_rows_with_another_fingerprint_are_not_used(sweep: ModuleType) -> None:
    """Decision 4: a screened model is ranked only when its fingerprint equals the sweep's."""
    fp = ("tree", "harness", "grading")
    sweep_study = study(sweep, "s", 7, fingerprint=fp)
    same = sweep.Study(
        "same", 1.0, (trial(sweep, 0, True, repeat=1), trial(sweep, 0, True, repeat=2)), fp
    )
    other = sweep.Study(
        "other",
        1.0,
        (trial(sweep, 0, True, repeat=1), trial(sweep, 0, True, repeat=2)),
        ("tree", "OLD", "g2"),
    )
    unknown = sweep.Study(
        "unknown", 1.0, (trial(sweep, 0, True, repeat=1), trial(sweep, 0, True, repeat=2)), None
    )
    keep, bad = sweep.usable([sweep_study, same, other, unknown])
    assert names(keep) == ["s", "same"]
    assert bad["other"] == ["harness_tree", "grading_sha256"]
    assert "unknown" in bad
    alone = sweep.usable([same])[1]
    assert "same" in alone, "with no sweep rows to compare, the reuse cannot be verified"


def test_markdown_lists_unusable_screen_rows(sweep: ModuleType) -> None:
    """The report says which screened models must be run in the sweep."""
    text = sweep.markdown(
        "tested-edit", sweep.rank([study(sweep, "a", 8)], N), {"x": ["harness_tree"]}
    )
    assert "x: harness_tree" in text and "silent_wrong" in text


def test_sizes_come_from_the_tags_document(sweep: ModuleType, tmp_path: Path) -> None:
    """Disk GiB per model from /api/tags, with `:latest` dropped; a file or a fetch function."""
    doc = {
        "models": [
            {"name": "glm-4.7-flash:latest", "size": 2**30 * 17},
            {"name": "a:1", "size": 2**29},
        ]
    }
    path = tmp_path / "tags.json"
    path.write_text(json.dumps(doc))
    assert sweep.read_sizes(path) == {"glm-4.7-flash": 17.0, "a:1": 0.5}
    assert sweep.read_sizes(None, lambda url: json.dumps(doc).encode())["glm-4.7-flash"] == 17.0


def test_find_raw_prefers_the_sibling_then_searches(sweep: ModuleType, tmp_path: Path) -> None:
    """A scored file beside its raw file uses it; one elsewhere finds the raw file holding the
    trial."""
    (tmp_path / "x.jsonl").write_text("{}\n")
    assert sweep.find_raw(tmp_path / "x.scored.jsonl", "t1", tmp_path) == tmp_path / "x.jsonl"
    out = tmp_path / "bakeoff"
    out.mkdir()
    raw = tmp_path / "y.jsonl"
    raw.write_text('{"trial_id": "t9"}\n')
    assert sweep.find_raw(out / "z.scored.jsonl", "t9", tmp_path) == raw
    with pytest.raises(sweep.Refusal):
        sweep.find_raw(out / "z.scored.jsonl", "nope", tmp_path)


def test_specified_trials_use_the_reports_own_functions(sweep: ModuleType) -> None:
    """The classifier is the report's: its infra_fault and analyse decide fault and byte_exact."""
    report = SimpleNamespace(
        infra_fault=lambda row, steps: row["trial_id"] == "f",
        analyse=lambda raw, scored, profiles, catalogue: {
            "byte_exact": {"k": int(scored[0]["trial_id"] == "ok")},
            "missing_answers": 0,
        },
    )
    raw = [{"trial_id": t, "steps": 3, "gpu": "100% GPU"} for t in ("ok", "bad", "f")]
    scored = [
        {"trial_id": t, "issue": "i", "repeat": 1, "secs": 5.0, "success": True}
        for t in ("ok", "bad", "f")
    ]
    got = sweep.specified_trials(raw, scored, report)
    assert [(t.primary, t.fault) for t in got] == [(True, False), (False, False), (False, True)]
    assert got[0].gpu == "100% GPU" and got[0].secondary


def test_grading_hash_is_the_last_registration(sweep: ModuleType) -> None:
    """The registry row of the experiment's last registration gives the grading hash."""
    rows = [
        {"event": "registered", "id": "e", "grading_sha256": "old"},
        {"event": "started", "id": "e"},
        {"event": "registered", "id": "e", "grading_sha256": "new"},
        {"event": "registered", "id": "f", "grading_sha256": "other"},
    ]
    assert sweep.grading_hash("e", rows) == "new" and sweep.grading_hash("zz", rows) == ""
