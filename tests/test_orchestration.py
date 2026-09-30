"""Tests for the orchestration layers with the runner, preflight and fixtures faked out.

Checks the parts that decide what gets measured: arm alternation, which prompt and model each
trial gets, what blocks a trial, what is cleaned up, and how records turn into scored rows.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bench import cli, gate, preflight, realism, reference, runner
from bench.issues import check, score, trials
from bench.rag import experiment

IDS = ("mut-helpers-60", "hand-injection-comment")


class FakeRunner:
    """Stands in for runner.run_trial: records what it was asked to run."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str, str]] = []

    def __call__(
        self,
        fx: Any,
        spec: runner.TrialSpec,
        artifacts: Path,
        trials_dir: Path,
        out: Path,
        **kw: Any,
    ) -> dict[str, Any]:
        self.calls.append((fx.version, spec.label, spec.arm, spec.agent, spec.model))
        with out.open("a") as fh:
            fh.write(json.dumps({"label": spec.label, "arm": spec.arm}) + "\n")
        return {"outcome": "completed"}


def mk(name: str, ids: tuple[str, ...] = IDS) -> runner.Fixture:
    manifest = {"tree_hash": "", "fixture_base_commit": ""}
    return runner.Fixture(name, Path("/nonexistent"), manifest, issue_ids=ids)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    fr = FakeRunner()
    monkeypatch.setattr(runner, "run_trial", fr)
    monkeypatch.setattr(preflight, "wait_clear", lambda *_a, **_k: [])
    return fr


def test_run_arms_interleaves_arms_flips_order_each_repeat_and_names_the_role_model(
    fake: FakeRunner, tmp_path: Path
) -> None:
    arms = {"baseline": mk("b"), "candidate": mk("c")}
    trials.run_arms(arms, 2, tmp_path / "o.jsonl")
    order = [(c[1], c[2]) for c in fake.calls]
    assert order[:4] == [(i, a) for i in IDS for a in ("baseline", "candidate")]
    assert order[4:] == [(i, a) for i in reversed(IDS) for a in ("candidate", "baseline")]
    by_issue = {c[1]: (c[3], c[4]) for c in fake.calls}
    assert by_issue["mut-helpers-60"] == ("coder", "devstral-small-2:24b")
    assert by_issue["hand-injection-comment"] == ("coder", "devstral-small-2:24b")


def test_a_single_arm_run_records_no_arm_and_only_filters_issues(
    fake: FakeRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "load", lambda _v, p: mk(p))
    trials.run("full", 1, tmp_path / "o.jsonl", only=["mut-helpers-60"])
    assert fake.calls == [("full", "mut-helpers-60", "", "coder", "devstral-small-2:24b")]


def test_a_blocked_host_stops_the_run_unless_forced(
    fake: FakeRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    busy = preflight.Problem("gpu_busy", "busy")
    monkeypatch.setattr(preflight, "wait_clear", lambda *_a, **_k: [busy])
    with pytest.raises(RuntimeError, match="gpu_busy"):
        trials.run_arms({"a": mk("a")}, 1, tmp_path / "o.jsonl")
    trials.run_arms({"a": mk("a")}, 1, tmp_path / "o.jsonl", force=True)
    assert len(fake.calls) == 2


def test_run_gate_passes_both_arms_and_the_output_to_the_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(cli, "load", lambda _v, p: mk(p))
    monkeypatch.setattr(
        trials, "run_arms", lambda arms, n, out, **kw: seen.update(arms=list(arms), n=n) or out
    )
    assert gate.run_gate("base", "cand", 3, tmp_path / "g.jsonl") == tmp_path / "g.jsonl"
    assert seen == {"arms": ["baseline", "candidate"], "n": 3}


def _scored(record: dict[str, Any]) -> score.IssueScore:
    return score.IssueScore(record["label"], "fix", "fixed", True, collateral=["x.py"])


def test_score_records_builds_one_row_per_scorable_trial_and_reports_unscorable_diffs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "load", lambda _v, p: mk(p))
    monkeypatch.setattr(check, "Env", lambda *a, **k: None)
    calls = {"n": 0}

    def fake_score(rec: dict[str, Any], _issues: Any, _env: Any) -> score.IssueScore:
        calls["n"] += 1
        if rec["trial_id"] == "bad":
            raise score.ScoreError("diff does not apply")
        return _scored(rec)

    monkeypatch.setattr(score, "score_record", fake_score)
    base = {"label": IDS[0], "model": "m", "agent": "coder", "outcome": "completed", "secs": 1.0}
    records = [
        {**base, "trial_id": "ok", "tool_calls": 3, "arm": "x", "answer_kind": "text"},
        {**base, "trial_id": "bad", "tool_calls": 1},
        {**base, "trial_id": "h", "tool_calls": 0, "outcome": "harness_error"},
        {**base, "trial_id": "u", "label": "not-an-issue", "tool_calls": 0},
    ]
    rows = trials.score_records(records, "full")
    assert [r["trial_id"] for r in rows] == ["ok", "bad"]
    assert rows[0]["success"] is True and rows[0]["arm"] == "x" and rows[0]["kind"]
    assert rows[1]["outcome"] == "unscorable" and "does not apply" in rows[1]["notes"][0]
    assert calls["n"] == 2


def test_score_file_and_the_report_command_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        trials,
        "score_records",
        lambda recs, _p: [{"issue": "a", "success": True, "kind": "k", "model": "m"} for _ in recs],
    )
    results = tmp_path / "r.jsonl"
    results.write_text('{"x": 1}\n{"x": 2}\n')
    out = trials.score_file(results, "full", tmp_path / "s.jsonl")
    assert len(trials.load_rows(out)) == 2
    assert trials.main(["report", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["n"] == 2
    monkeypatch.setattr(trials, "run", lambda *a, **k: out)
    assert trials.main(["run", "--profile", "full", "--n", "1", "--out", str(tmp_path / "o")]) == 0


def test_gate_main_judges_and_exits_nonzero_unless_cleared(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = [
        {"arm": a, "issue": f"i{k}", "success": True, "outcome": "fixed"}
        for a in ("baseline", "candidate")
        for k in range(3)
    ]
    monkeypatch.setattr(gate, "score_arms", lambda *_a: rows)
    results = tmp_path / "r.jsonl"
    results.write_text("")
    assert gate.main(["judge", str(results), "--baseline", "a", "--candidate", "b"]) == 1
    assert json.loads(capsys.readouterr().out)["verdict"] == "INCONCLUSIVE"
    monkeypatch.setattr(gate, "run_gate", lambda *a, **k: results)
    assert gate.main(["run", "--baseline", "a", "--candidate", "b", "--out", str(results)]) == 0


def test_score_arms_scores_each_arm_against_its_own_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profiles: list[tuple[str, int]] = []

    def fake(records: list[dict[str, Any]], profile: str) -> list[dict[str, Any]]:
        profiles.append((profile, len(records)))
        return [{"arm": r["arm"], "issue": r["label"]} for r in records]

    monkeypatch.setattr(trials, "score_records", fake)
    results = tmp_path / "r.jsonl"
    results.write_text(
        "\n".join(
            json.dumps({"arm": a, "label": "x"}) for a in ("baseline", "candidate", "baseline")
        )
    )
    rows = gate.score_arms(results, "p-base", "p-cand")
    assert profiles == [("p-base", 2), ("p-cand", 1)] and len(rows) == 3


def test_run_study_alternates_sides_and_always_discards_the_real_code_copy(
    fake: FakeRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    monkeypatch.setattr(cli, "load", lambda _v, _p: mk("fixture"))
    monkeypatch.setattr(reference, "discard", lambda _d: events.append("discard"))
    monkeypatch.setattr(reference, "prepare", lambda *_a: tmp_path / "tree")

    def fake_reference(spec: runner.TrialSpec, *_a: Any, **_k: Any) -> dict[str, Any]:
        events.append(f"ref:{spec.label}")
        return {"outcome": "completed"}

    monkeypatch.setattr(reference, "run_reference", fake_reference)
    realism.run_study(2, tmp_path / "s.jsonl")
    fixture_labels = [c[1] for c in fake.calls]
    ref_labels = [e[4:] for e in events if e.startswith("ref:")]
    assert len(fixture_labels) == len(ref_labels) == 2 * len(realism.TASKS)
    assert events[0] == "discard" and events[-1] == "discard"

    def boom(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise RuntimeError("agent exploded")

    events.clear()
    monkeypatch.setattr(reference, "run_reference", boom)
    with pytest.raises(RuntimeError):
        realism.run_study(1, tmp_path / "s2.jsonl")
    assert events[-1] == "discard", "the copy of real code is removed even when a trial fails"


def test_realism_main_reports_a_results_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "r.jsonl"
    results.write_text("")
    assert realism.main(["--report", "--out", str(results)]) == 0
    assert "metric" in capsys.readouterr().out


def test_rag_experiment_runs_each_question_in_both_arms_and_flips_order(
    fake: FakeRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    qs = [
        experiment.Question("q1", "What?", ("a",), ()),
        experiment.Question("q2", "Why?", ("b",), ()),
    ]
    monkeypatch.setattr(cli, "load", lambda _v, _p: mk("fx"))
    monkeypatch.setattr(experiment, "select_questions", lambda *_a: qs)
    experiment.run(2, tmp_path / "r.jsonl", 2)
    arms = [c[2] for c in fake.calls]
    assert arms == ["control", "treatment"] * 2 + ["treatment", "control"] * 2
    assert {c[3] for c in fake.calls} == {"research"}


def test_rag_experiment_main_analyses_a_results_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "load", lambda _v, _p: mk("fx"))
    monkeypatch.setattr(experiment, "select_questions", lambda *_a: [])
    results = tmp_path / "r.jsonl"
    results.write_text("")
    assert experiment.main(["analyse", str(results)]) == 0
    assert "not evaluable" in capsys.readouterr().out
    monkeypatch.setattr(experiment, "run", lambda *a, **k: results)
    assert experiment.main(["run", "--n", "1", "--out", str(results)]) == 0
