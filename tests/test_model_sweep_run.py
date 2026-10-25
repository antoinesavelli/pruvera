"""The model-sweep driver stops a study on the failure that puts the floor out of reach, and only then.

Depends on: spikes/model_sweep_run.py, pytest.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def run() -> ModuleType:
    """Loads the driver spike as a module."""
    path = ROOT / "spikes" / "model_sweep_run.py"
    spec = importlib.util.spec_from_file_location("spike_model_sweep_run", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def ids(n: int) -> list[str]:
    """Issue ids i0..i(n-1)."""
    return [f"i{k}" for k in range(n)]


class Script:
    """A `run_one` that answers from a script and records which issues it was asked to run."""

    def __init__(self, answers: dict[str, list[bool | None]]) -> None:
        self.answers = {k: list(v) for k, v in answers.items()}
        self.calls: list[str] = []

    def __call__(self, issue: str) -> bool | None:
        self.calls.append(issue)
        queue = self.answers.get(issue, [True])
        return queue.pop(0) if len(queue) > 1 else queue[0]


def test_the_limit_is_n_minus_the_floor_plus_one(run: ModuleType) -> None:
    """3 scorable failures at N = 8 or 10, 2 at N = 7, 4 at N = 12."""
    limits = {n: run.stop_decision(n, []).limit for n in (7, 8, 9, 10, 12)}
    assert limits == {7: 2, 8: 3, 9: 3, 10: 3, 12: 4}


def test_it_stops_on_the_third_failure_not_the_second(run: ModuleType) -> None:
    """At N = 8: two failures leave the floor reachable, the third does not."""
    assert not run.stop_decision(8, [False, True, False, True]).stop
    decision = run.stop_decision(8, [False, True, False, False])
    assert decision.stop and (decision.failures, decision.scored, decision.floor) == (3, 4, 6)


def test_faults_never_count_toward_the_stop(run: ModuleType) -> None:
    """An infrastructure fault is neither a failure nor a success, however many there are."""
    assert not run.stop_decision(8, [None, None, None, None, False, False]).stop
    assert run.stop_decision(8, [None, False, None, False, None, False]).stop


def test_a_study_stops_at_the_third_failure_and_runs_nothing_more(run: ModuleType) -> None:
    """Issues after the stopping failure are not run; the result says it stopped."""
    script = Script({"i0": [False], "i1": [True], "i2": [False], "i3": [False]})
    result = run.drive_study(ids(8), {}, script)
    assert script.calls == ["i0", "i1", "i2", "i3"]
    assert result.stopped and sum(result.outcomes.values()) == 1 and len(result.outcomes) == 4


def test_a_study_that_can_reach_the_floor_runs_to_the_end(run: ModuleType) -> None:
    """Two failures at N = 8 do not stop it; it finishes and is not marked stopped."""
    script = Script({"i0": [False], "i3": [False]})
    result = run.drive_study(ids(8), {}, script)
    assert script.calls == ids(8) and not result.stopped and sum(result.outcomes.values()) == 6


def test_a_fault_is_rerun_and_does_not_stop_the_study(run: ModuleType) -> None:
    """A faulted issue is run again; the study goes on and the fault is counted apart."""
    script = Script({"i0": [None, None, True], "i1": [False], "i2": [False]})
    result = run.drive_study(ids(8), {}, script)
    assert script.calls.count("i0") == 3 and not result.stopped and result.faults == 2
    assert len(result.outcomes) == 8


def test_an_issue_that_keeps_faulting_is_left_unscored_not_scored(run: ModuleType) -> None:
    """After the attempts the issue has no outcome (never a failure) and the rest still runs."""
    script = Script({"i1": [None]})
    result = run.drive_study(ids(4), {}, script, attempts=2)
    assert result.unscored == ("i1",) and "i1" not in result.outcomes and result.faults == 2
    assert script.calls == ["i0", "i1", "i1", "i2", "i3"]


def test_it_resumes_without_rerunning_recorded_cells(run: ModuleType) -> None:
    """Cells already scorable are never run again; only the rest is."""
    script = Script({})
    result = run.drive_study(ids(8), {"i0": True, "i1": False, "i2": True}, script)
    assert script.calls == ["i3", "i4", "i5", "i6", "i7"]
    assert len(result.outcomes) == 8


def test_a_resumed_study_that_already_hit_the_limit_runs_nothing(run: ModuleType) -> None:
    """Three recorded failures: the resume stops at once and runs no trial."""
    script = Script({})
    result = run.drive_study(ids(8), {"i0": False, "i1": False, "i2": False}, script)
    assert script.calls == [] and result.stopped


def test_with_lever_two_off_nothing_stops_early(run: ModuleType) -> None:
    """Without the stop rule a failing model runs every issue."""
    script = Script({f"i{k}": [False] for k in range(8)})
    result = run.drive_study(ids(8), {}, script, lever2=False)
    assert script.calls == ids(8) and not result.stopped


def test_a_study_that_fails_its_last_issues_is_complete_not_stopped(run: ModuleType) -> None:
    """Failures on the final issue reach the limit with nothing left to skip: finished, not stopped."""
    script = Script({"i5": [False], "i6": [False], "i7": [False]})
    result = run.drive_study(ids(8), {}, script)
    assert script.calls == ids(8) and not result.stopped


def test_ids_files_are_read_like_campaign_sh(run: ModuleType, tmp_path: Path) -> None:
    """One id a line, comments and blanks skipped; a foreign or repeated id is refused."""
    path = tmp_path / "x.ids"
    path.write_text("# lever 1\nsweep-specified-edit-a-b\n\nsweep-tested-edit-a-b  # both\n")
    assert run.read_ids(path) == ["sweep-specified-edit-a-b", "sweep-tested-edit-a-b"]
    assert [run.kind_of(i) for i in run.read_ids(path)] == ["specified-edit", "tested-edit"]
    path.write_text("kinds-granite4-1-8b\n")
    with pytest.raises(ValueError):
        run.read_ids(path)
    path.write_text("sweep-tested-edit-a\nsweep-tested-edit-a\n")
    with pytest.raises(ValueError):
        run.read_ids(path)


def test_the_timing_gate_keeps_the_first_measurement(run: ModuleType) -> None:
    """Lever 2 stays on only if scoring one row is within the limit; later rows do not move the figure."""
    fast = run.Timer(15.0)
    assert fast.record(4.0) and fast.record(40.0) and fast.seconds == 4.0
    slow = run.Timer(15.0)
    assert not slow.record(20.0) and slow.seconds == 20.0
