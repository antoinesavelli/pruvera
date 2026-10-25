"""What `experiment.judge`, `interim` and `run` refuse before a verdict of record (review round 9b).

No verdict on rows nobody graded or lost; no judge of an unfinished design (safety repeats too), on
another fixture version, or on grading code that changed unexplained; no rerun of a judged study;
confirmatory code pinned to the registered `bench/` tree; the alpha share re-checked under the lock.
Depends on: bench.{budget,experiment,gate,layout,registry}, bench.issues.trials,
tests.test_decisions (the registered study and its simulated rows), pytest.
"""

from __future__ import annotations

import json
import random
import subprocess
from pathlib import Path
from typing import Any

import pytest

from bench import budget, experiment, gate, layout, registry
from bench.issues import trials
from tests.helpers import spec as _spec
from tests.test_decisions import STRONG, _register, _scored


def _study(
    root: Path, monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]], extra: str = ""
) -> None:
    """A registered, started confirmatory study whose scoring is stubbed to `rows`."""
    _register(root, monkeypatch, STRONG, extra)
    results = registry.results_dir(root) / "experiments" / "hold-1.jsonl"
    results.parent.mkdir(parents=True, exist_ok=True)
    results.write_text(json.dumps({"trial_id": "t1", "model": "m", "artifact_sha256": "a"}) + "\n")
    monkeypatch.setattr(gate, "score_arms", lambda *_a, **_k: rows)
    registry.append(root, "started", id="hold-1", out="results/experiments/hold-1.jsonl")


def _judged_rows(root: Path) -> list[dict[str, Any]]:
    """The study's `judged` rows."""
    return [r for r in registry.read(root) if r["event"] == "judged"]


@pytest.mark.parametrize("outcome", ["unscorable", "harness_error"])
def test_judge_and_interim_refuse_while_a_row_is_unscorable_or_lost(
    root: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    """One unscorable or harness-error row in either arm refuses judge and interim, naming it."""
    rows = _scored(random.Random(3))
    k = next(i for i, r in enumerate(rows) if r["arm"] == "candidate" and r["repeat"] == 1)
    rows[k] = {**rows[k], "outcome": outcome, "success": False}
    _study(root, monkeypatch, rows, "interim = [1]\n")
    named = (
        "unscorable rows \\(baseline/candidate 0/1\\)"
        if outcome == "unscorable"
        else ("1 harness errors \\(0/1\\)")
    )
    for call in (
        lambda: experiment.judge("hold-1", root),
        lambda: experiment.interim("hold-1", 1, root),
    ):
        with pytest.raises(registry.RegistryError, match=named):
            call()
    assert _judged_rows(root) == [] and not any(
        r["event"] == "interim" for r in registry.read(root)
    )


def _safety_design(rows_through: int) -> list[dict[str, Any]]:
    """Rows of a 4/8 design (safety issues i0-i2 get 8 repeats) stopped after `rows_through`."""
    rows = []
    for arm in ("baseline", "candidate"):
        for k in range(12):
            safety = k < 3
            for rep in range(min(8 if safety else 4, rows_through)):
                rows.append({"arm": arm, "issue": f"i{k}", "repeat": rep + 1, "success": True,
                             "outcome": "asked" if safety else "fixed",
                             "expected": "ask_first" if safety else "fix"})  # fmt: skip
    return rows


def test_judge_refuses_a_design_whose_safety_repeats_are_unfinished(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 4/8 study stopped after repeat 6 is refused per issue; finished, it is judged (probe 2)."""
    planned = {f"i{k}": 8 if k < 3 else 4 for k in range(12)}
    monkeypatch.setattr(gate, "issue_repeats", lambda *_a, **_k: planned)
    _study(root, monkeypatch, _safety_design(6), "safety_repeats = 8\n")
    with pytest.raises(registry.RegistryError, match=r"6 issue cells .*first: baseline i0 6/8"):
        experiment.judge("hold-1", root)
    monkeypatch.setattr(gate, "score_arms", lambda *_a, **_k: _safety_design(8))
    assert experiment.judge("hold-1", root)["verdict"]


def test_an_issue_with_no_trials_is_unfinished_unless_a_proof_skips_it(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A profile issue absent from both arms blocks the judge; a proof-skipped one does not."""
    rows = [r for r in _scored(random.Random(3)) if r["issue"] != "i11"]
    _study(root, monkeypatch, rows)
    monkeypatch.setattr(experiment, "_unwinnable", lambda _row: set())
    with pytest.raises(registry.RegistryError, match="2 issue cells .*baseline i11 0/4"):
        experiment.judge("hold-1", root)
    monkeypatch.setattr(experiment, "_unwinnable", lambda _row: {"i11"})
    assert experiment.judge("hold-1", root)["verdict"]


def test_judge_and_interim_refuse_another_fixture_version(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A study registered on v2 is neither judged nor looked at by a process that selects v4."""
    monkeypatch.setattr(layout, "VERSION", "v2")
    _study(root, monkeypatch, _scored(random.Random(3)), "interim = [1]\n")
    monkeypatch.setattr(layout, "VERSION", "v4")
    for call in (
        lambda: experiment.judge("hold-1", root),
        lambda: experiment.interim("hold-1", 1, root),
    ):
        with pytest.raises(registry.RegistryError, match="registered on fixture v2"):
            call()


def test_a_judged_study_cannot_run_again_and_its_state_says_judged(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After judge, `run` and `authorize` refuse the id and `status` shows it as judged."""
    _study(root, monkeypatch, _scored(random.Random(3)))
    assert experiment.status(root)[0]["state"] == "started"
    experiment.judge("hold-1", root)
    with pytest.raises(registry.RegistryError, match="already judged"):
        experiment.run("hold-1", root=root)
    with pytest.raises(registry.RegistryError, match="already judged"):
        registry.authorize("hold-1", {"b": "holdout"}, None, root / "o.jsonl", root)
    assert experiment.status(root)[0]["state"] == "judged"


def test_changed_grading_code_refuses_a_confirmatory_judge_unless_a_reason_is_recorded(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The grading hash is registered; a changed one needs a reason, kept in row and report."""
    _study(root, monkeypatch, _scored(random.Random(3)))
    row = registry.registered("hold-1", root)
    assert row["grading_sha256"] == experiment.grading_hash()
    monkeypatch.setattr(experiment, "grading_hash", lambda: "0" * 64)
    with pytest.raises(registry.RegistryError, match="--accept-scorer-change"):
        experiment.judge("hold-1", root)
    experiment.judge("hold-1", root, accept_scorer_change="multi-file fix rule")
    judged = _judged_rows(root)[0]
    assert judged["scorer_change_accepted"] == "multi-file fix rule"
    text = (registry.results_dir(root) / "verdicts" / "hold-1" / "REPORT.md").read_text()
    assert "grading code changed since registration; accepted: multi-file fix rule" in text


def test_a_registration_without_the_grading_hash_or_a_non_confirmatory_change_gets_a_caveat(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row with no grading hash gets a caveat; a non-confirmatory change, a caveat only."""
    assert experiment._grading_note({"kind": "confirmatory", "id": "x"}, "") == (
        "grading code not recorded at registration"
    )
    assert (
        experiment._grading_note({"kind": "exploratory", "id": "x", "grading_sha256": "0"}, "")
        == "grading code changed since registration"
    )


def _commit_bench(root: Path, text: str) -> None:
    """Commits a change under bench/ in the test repo."""
    (root / "bench" / "x.py").write_text(text)
    subprocess.run(["git", "-C", str(root), "commit", "-qam", "bench change"], check=True)


def test_a_confirmatory_run_or_resume_on_another_bench_tree_is_refused(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registered and started rows carry the bench/ tree; a confirmatory run off it is refused."""
    row = _register(root, monkeypatch, STRONG)
    assert row["harness_tree"] not in ("", "unknown")
    out, arms = root / "o.jsonl", {"b": "holdout", "c": "holdout+rule"}
    registry.authorize("hold-1", arms, None, out, root)
    assert [r["harness_tree"] for r in registry.read(root) if r["event"] == "started"] == [
        row["harness_tree"]
    ]
    _commit_bench(root, "x = 2\n")
    with pytest.raises(registry.RegistryError, match="registered on"):
        registry.authorize("hold-1", arms, None, out, root)
    _commit_bench(root, "x = 1\n")  # the same code again: the same tree, whatever the commit
    assert registry.authorize("hold-1", arms, None, out, root) == "hold-1"


def test_an_exploratory_run_on_another_tree_runs_and_the_report_names_both_trees(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a confirmatory study is pinned; any study's caveats list the trees its trials ran on."""
    registry.register(_spec(root, "dev-1", "exploratory", "dev", "dev+rule"), root)
    _commit_bench(root, "x = 3\n")
    assert registry.authorize("dev-1", {"b": "dev", "c": "dev+rule"}, None, root / "o", root)
    row = registry.registered("dev-1", root)
    notes = experiment._caveats(row, {}, [{"harness_tree": "f" * 40}])
    assert any("span 2 bench/ trees" in n for n in notes)
    assert not any("trees" in n for n in experiment._caveats(row, {}, [{}]))


def test_the_alpha_share_is_checked_again_under_the_lock(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registration whose alpha share a rival spent meanwhile is refused and writes nothing."""
    monkeypatch.setattr(budget, "power", lambda *_a, **_k: STRONG)
    real = budget.alpha_for
    calls: list[int] = []

    def racing(rows: list[dict[str, Any]], gens: set[str], r: Path) -> float:
        """The share the power check sees first; under the lock a rival has taken half of it."""
        calls.append(len(rows))
        return real(rows, gens, r) / (1 if len(calls) == 1 else 2)

    monkeypatch.setattr(budget, "alpha_for", racing)
    spec = _spec(root, "hold-1", "confirmatory", "holdout", "holdout+rule")
    with pytest.raises(registry.RegistryError, match="spent alpha in this generation meanwhile"):
        experiment.register(spec, root)
    assert registry.read(root) == [] and len(calls) == 2


def test_a_cell_that_never_succeeded_keeps_one_row_its_last_attempt() -> None:
    """Three lost attempts at a cell leave one row, the last; a redone cell keeps only its redo."""

    def rec(trial_id: str, outcome: str, repeat: int = 1) -> dict[str, Any]:
        """A trial record of experiment e1, issue i1, baseline."""
        return {"experiment_id": "e1", "label": "i1", "arm": "baseline", "repeat": repeat,
                "trial_id": trial_id, "outcome": outcome}  # fmt: skip

    records = [rec("a", "readback_failed"), rec("b", "harness_error"), rec("c", "readback_failed"),
               rec("d", "harness_error", 2), rec("e", "fixed", 2)]  # fmt: skip
    assert [r["trial_id"] for r in trials.without_redone(records)] == ["c", "e"]


def test_a_stale_build_whose_issues_differ_from_its_profile_toml_is_refused(root: Path) -> None:
    """`run` refuses a built profile planting other issues than the TOML the registration read."""
    from bench import runner

    stale = runner.Fixture("v2", Path("/nonexistent"), {}, profile="dev", issue_ids=("d1", "d2"))
    with pytest.raises(registry.RegistryError, match="stale build"):
        experiment._check_build(stale, root)
    experiment._check_build(
        runner.Fixture("v2", Path("/x"), {}, profile="dev", issue_ids=("d1",)), root
    )


def test_the_design_is_read_from_the_root_it_is_given(tmp_path: Path) -> None:
    """`gate.issue_repeats` reads the profile and catalogue under `root`, not the code root."""
    (tmp_path / "issues" / "profiles").mkdir(parents=True)
    (tmp_path / "issues" / "profiles" / "p.toml").write_text('description = "p"\nissues = []\n')
    assert gate.issue_repeats("p", 4, 8, tmp_path) == {}
    with pytest.raises(FileNotFoundError):
        gate.issue_repeats("p", 4, 8)  # no profile p under the code root
