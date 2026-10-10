"""The repeats `experiment.run` executes: each issue's registered count, not one flat number.

A spec's `safety_repeats` gives every safety issue more repeats than `repeats`. The run executes
the design the registration power-checked (`gate.issue_repeats`), resumably, in the arm order the
campaign always uses; `--n` stays a flat override.

Depends on: bench.{budget,experiment,gate,registry,runner}, bench.issues.trials,
tests.test_experiment_seed (the recording runner and fixture), pytest.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from bench import budget, experiment, gate, preflight, registry, runner
from bench.issues import trials
from tests.helpers import needs_catalogue
from tests.helpers import spec as _spec
from tests.test_experiment_seed import IDS, Recorder, _fixture

PLAIN, SAFETY = IDS  # a `fix` issue and an `ignore` (safety) one, both in the catalogue
EXTRA = "safety_repeats = 6\n"  # the spec template runs 4 repeats
STRONG = {"clear_if_harmless": 0.9, "reject_if_loss": 0.9, "alpha": 0.025, "reps": 100}
Asked = list[tuple[str, int, int | None]]
Campaign = tuple[Recorder, list[str]]


@pytest.fixture
def campaign(monkeypatch: pytest.MonkeyPatch) -> Campaign:
    """A run whose trials are recorded, not run; the second item lists the studies authorized."""
    rec, authorized = Recorder(), []

    def authorize(experiment_id: str, *_a: object, **_k: object) -> str:
        authorized.append(experiment_id)
        return experiment_id

    monkeypatch.setattr(runner, "run_trial", rec)
    monkeypatch.setattr(preflight, "wait_clear", lambda *_a, **_k: [])
    monkeypatch.setattr(registry, "authorize", authorize)
    return rec, authorized


@pytest.fixture
def design(monkeypatch: pytest.MonkeyPatch) -> Asked:
    """The registered design stubbed to the two catalogue issues; records what a run asked of it."""
    asked: Asked = []

    def issue_repeats(
        profile: str, repeats: int, safety_repeats: int | None = None
    ) -> dict[str, int]:
        asked.append((profile, repeats, safety_repeats))
        return {PLAIN: repeats, SAFETY: safety_repeats or repeats}

    monkeypatch.setattr(gate, "issue_repeats", issue_repeats)
    monkeypatch.setattr(runner, "load_profile", lambda _v, p: _fixture(p))
    return asked


@needs_catalogue
def test_the_repeats_a_run_plans_are_the_design_the_power_check_scores() -> None:
    plan = gate.issue_repeats("dev", 3, 5)
    shape = gate.design_of("dev", 3, 5)
    assert tuple(plan.values()) == shape.repeats
    assert list(plan.values()).count(5) == shape.safety.count(True) > 0
    assert set(plan.values()) == {3, 5} and set(gate.issue_repeats("dev", 3).values()) == {3}


@needs_catalogue
def test_run_arms_gives_an_issue_its_own_count_and_the_others_n(
    campaign: Campaign,
    tmp_path: Path,
) -> None:
    rec, _ = campaign
    trials.run_arms({"a": _fixture("a")}, 2, tmp_path / "more.jsonl", repeats={SAFETY: 3})
    assert Counter(c[0] for c in rec.cells) == {PLAIN: 2, SAFETY: 3}
    rec.cells.clear()
    trials.run_arms({"a": _fixture("a")}, 2, tmp_path / "less.jsonl", repeats={SAFETY: 1})
    assert Counter(c[0] for c in rec.cells) == {PLAIN: 2, SAFETY: 1}


@needs_catalogue
def test_run_gives_the_safety_issues_their_extra_repeats_and_keeps_the_interleave_and_flip(
    root: Path,
    campaign: Campaign,
    design: Asked,
) -> None:
    rec, authorized = campaign
    registry.register(_spec(root, "e-1", "exploratory", "dev", "dev+rule", extra=EXTRA), root)
    experiment.run("e-1", root=root)
    assert design == [("dev", 4, 6)] and authorized == ["e-1"]
    assert Counter(c[0] for c in rec.cells) == {PLAIN: 4 * 2, SAFETY: 6 * 2}  # two arms
    cells = [(c[0], c[1], c[2]) for c in rec.cells]
    assert cells[:4] == [(i, a, 1) for i in (PLAIN, SAFETY) for a in ("baseline", "candidate")]
    assert cells[-4:] == [  # repeats 5 and 6 are the safety issue's alone; the order flips on 6
        (SAFETY, "baseline", 5), (SAFETY, "candidate", 5),
        (SAFETY, "candidate", 6), (SAFETY, "baseline", 6),
    ]  # fmt: skip


@needs_catalogue
def test_a_rerun_skips_the_cells_done_and_a_killed_run_resumes_at_the_missing_one(
    root: Path,
    campaign: Campaign,
    design: Asked,
) -> None:
    rec, _ = campaign
    registry.register(_spec(root, "e-1", "exploratory", "dev", "dev+rule", extra=EXTRA), root)
    experiment.run("e-1", root=root)
    done = len(rec.cells)
    experiment.run("e-1", root=root)
    assert done == 4 * 2 + 6 * 2 == len(rec.cells)
    results = registry.results_dir(root) / "experiments" / "e-1.jsonl"
    results.write_text("".join(results.read_text().splitlines(keepends=True)[:-1]))  # a kill
    experiment.run("e-1", root=root)
    assert rec.cells[done:] == [rec.cells[done - 1]]


@needs_catalogue
def test_n_is_a_flat_override_so_the_old_second_command_still_works_and_the_two_agree(
    root: Path,
    campaign: Campaign,
    design: Asked,
) -> None:
    rec, _ = campaign
    registry.register(_spec(root, "e-1", "exploratory", "dev", "dev+rule", extra=EXTRA), root)
    experiment.run("e-1", root=root, n=4)
    first = len(rec.cells)
    experiment.run("e-1", root=root, n=6, only=[SAFETY])
    assert sorted((c[0], c[2]) for c in rec.cells[first:]) == [(SAFETY, r) for r in (5, 5, 6, 6)]
    assert design == []  # --n never reads the design
    experiment.run("e-1", root=root)  # the design is now complete, so a plain run has nothing to do
    assert len(rec.cells) == first + 4 and design == [("dev", 4, 6)]


@needs_catalogue
def test_a_confirmatory_study_runs_its_whole_registered_design_with_no_flags(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    campaign: Campaign,
    design: Asked,
) -> None:
    rec, _ = campaign
    monkeypatch.setattr(budget, "power", lambda *_a, **_k: STRONG)
    spec = _spec(root, "hold-1", "confirmatory", "holdout", "holdout+rule", extra=EXTRA)
    experiment.register(spec, root)
    experiment.run("hold-1", root=root)
    assert design == [("holdout", 4, 6)]
    assert Counter(c[0] for c in rec.cells) == {PLAIN: 4 * 2, SAFETY: 6 * 2}


@needs_catalogue
def test_a_study_without_safety_repeats_runs_its_flat_repeats_and_never_reads_the_design(
    root: Path,
    campaign: Campaign,
    design: Asked,
) -> None:
    rec, _ = campaign
    registry.register(_spec(root, "e-1", "exploratory", "dev", "dev+rule"), root)
    experiment.run("e-1", root=root)
    assert design == [] and Counter(c[0] for c in rec.cells) == {PLAIN: 4 * 2, SAFETY: 4 * 2}
    assert (
        experiment._planned({"baseline": "dev", "repeats": 4}, None) is None
    )  # a row before the field
