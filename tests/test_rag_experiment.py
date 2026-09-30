"""Tests for the retrieval A/B: question selection, grading, transcripts, the verdict rule."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("yaml")

from bench import cli
from bench.rag import experiment as ex  # noqa: E402


def write_tree(tmp_path: Path) -> Path:
    tree = tmp_path / "tree"
    (tree / "docs/eval").mkdir(parents=True)
    (tree / "docs/STATUS.md").write_text(
        "The operative default is insider_cluster, a long strategy."
    )
    (tree / ex.QUESTION_FILE).write_text(
        """
- id: ok
  question: "Which strategy is the default and its side?"
  must_contain: ["insider_cluster", "long"]
  must_not_contain: ["momentum is the default"]
  sources: [docs/STATUS.md]
- id: echoes-question
  question: "Is insider_cluster long?"
  must_contain: ["insider_cluster", "long"]
  sources: [docs/STATUS.md]
- id: fact-not-in-docs
  question: "What is the kill switch command?"
  must_contain: ["paramo-kill"]
  sources: [docs/STATUS.md]
- id: missing-source
  question: "Anything?"
  must_contain: ["x"]
  sources: [docs/GONE.md]
"""
    )
    return tree


def test_only_questions_the_docs_answer_and_the_question_does_not_give_away_are_selected(
    tmp_path: Path,
) -> None:
    assert [q.id for q in ex.select_questions(write_tree(tmp_path))] == ["ok"]


def test_selection_samples_evenly_and_deterministically(tmp_path: Path) -> None:
    tree = write_tree(tmp_path)
    entries = "\n".join(
        f'- id: q{i:02d}\n  question: "Which one?"\n  must_contain: ["insider_cluster"]\n'
        "  sources: [docs/STATUS.md]"
        for i in range(10)
    )
    (tree / ex.QUESTION_FILE).write_text(entries)
    picked = [q.id for q in ex.select_questions(tree, limit=5)]
    assert picked == ["q00", "q02", "q04", "q06", "q08"]
    assert picked == [q.id for q in ex.select_questions(tree, limit=5)]


def test_grade_needs_every_required_pattern_and_no_forbidden_one() -> None:
    q = ex.Question("x", "?", ("insider_cluster", "long"), ("momentum is the default",))
    assert ex.grade("Insider_Cluster, a LONG strategy", q)
    assert not ex.grade("insider_cluster", q)
    assert not ex.grade("insider_cluster long; momentum is the default", q)


def make_artifact(
    tmp_path: Path, name: str, answer: str, tool_inputs: list[dict[str, str]]
) -> Path:
    art = tmp_path / name
    art.mkdir()
    events = [
        {"type": "tool_use", "part": {"tool": "grep", "state": {"input": i}}} for i in tool_inputs
    ]
    events.append({"type": "text", "part": {"text": answer}})
    (art / "transcript.jsonl").write_text("\n".join(json.dumps(e) for e in events))
    return art


def test_answer_key_contact_is_detected_from_tool_inputs(tmp_path: Path) -> None:
    art = make_artifact(
        tmp_path, "a", "done", [{"pattern": "x", "path": "docs/eval/knowledge_questions.yaml"}]
    )
    answer, tools = ex.read_transcript(art)
    assert answer == "done" and ex.touched_answer_key(tools)
    clean = make_artifact(tmp_path, "b", "done", [{"pattern": "STATUS"}])
    assert not ex.touched_answer_key(ex.read_transcript(clean)[1])


def test_treatment_adds_the_search_server_and_control_does_not(tmp_path: Path) -> None:
    q = ex.Question("x", "Which?", ("a",), ())
    control = ex.arm_spec("control", q, tmp_path)
    treatment = ex.arm_spec("treatment", q, tmp_path)
    assert control.inline == {} and control.extra_binds == ()
    server = treatment.inline["mcp"]["docsearch"]
    assert server["command"][1] == "/opt/rag/server.py" and server["type"] == "local"
    assert (tmp_path, "/opt/rag/index") in treatment.extra_binds
    assert control.prompt == treatment.prompt and control.model == treatment.model


def arm(n: int, correct: int, errors: int) -> dict[str, int]:
    return {
        "n": n,
        "correct": correct,
        "clean_n": n,
        "clean_correct": correct,
        "tool_errors": errors,
        "searches": 0,
    }


WIDE = (0.0, -0.3, 0.3)  # a clustered interval far wider than the margin
TIGHT = (0.2, 0.1, 0.3)


def test_verdict_follows_the_pre_registered_rule_and_says_when_the_data_are_too_thin() -> None:
    adopt = ex.verdict({"control": arm(36, 20, 2), "treatment": arm(36, 27, 2)}, TIGHT)
    assert adopt.startswith("ADOPT") and "too little data" not in adopt
    thin = ex.verdict({"control": arm(36, 20, 2), "treatment": arm(36, 27, 2)}, WIDE)
    assert thin.startswith("ADOPT") and "too little data" in thin
    assert ex.verdict({"control": arm(36, 20, 2), "treatment": arm(36, 22, 2)}, TIGHT).startswith(
        "DO NOT"
    )
    assert ex.verdict({"control": arm(36, 20, 2), "treatment": arm(36, 30, 9)}, TIGHT).startswith(
        "DO NOT"
    )
    nan = float("nan")
    assert "too little data" in ex.verdict(
        {"control": arm(3, 1, 0), "treatment": arm(3, 3, 0)}, (nan, nan, nan)
    )
    assert "not evaluable" in ex.verdict({"control": arm(36, 20, 2)}, TIGHT)


def graded(tmp_path: Path, trials: list[tuple[str, str, list[dict[str, str]]]]) -> Path:
    """A results file from (arm, final answer, tool inputs) triples, all for question `ok`."""
    rows = []
    for i, (arm_name, answer, tools) in enumerate(trials):
        art = make_artifact(tmp_path, f"t{i}", answer, tools)
        rows.append(
            {
                "trial_id": f"t{i}",
                "label": "ok",
                "arm": arm_name,
                "artifact": str(art),
                "tool_errors": 0,
            }
        )
    results = tmp_path / "r.jsonl"
    results.write_text("\n".join(json.dumps(r) for r in rows))
    return results


Q = ex.Question("ok", "?", ("insider_cluster",), ())
NOTHING = [{"pattern": "x"}]


def test_grade_rows_store_each_trials_grade_answer_kind_and_repeat_number(tmp_path: Path) -> None:
    results = graded(
        tmp_path,
        [
            ("control", "no idea", NOTHING),
            ("control", "", NOTHING),
            ("control", '{"name": "read", "arguments": {}}', NOTHING),
            ("treatment", "insider_cluster", NOTHING),
        ],
    )
    rows = ex.grade_rows(results, [Q])
    assert [r["rep"] for r in rows] == [1, 2, 3, 1]
    assert [r["answer_kind"] for r in rows] == ["text", "empty", "tool_json", "text"]
    assert [r["correct"] for r in rows] == [False, False, False, True]


def test_non_answers_are_counted_apart_from_wrong_answers(tmp_path: Path) -> None:
    """Regression: empty answers and raw tool JSON were silently counted as completed trials."""
    trials = [("control", "", NOTHING)] * 4 + [("control", "wrong", NOTHING)] * 2
    trials += [("treatment", "insider_cluster", NOTHING)] * 6
    out = ex.analyse(ex.grade_rows(graded(tmp_path, trials), [Q]))
    control = out["arms"]["control"]
    assert (
        control["non_answers"] == 4
        and control["answered_n"] == 2
        and control["answered_correct"] == 0
    )
    assert out["arms"]["treatment"]["non_answers"] == 0
    assert out["clustered_diff"]["diff"] == 1.0


def test_the_repeat_breakdown_exposes_a_first_repeat_effect(tmp_path: Path) -> None:
    """Regression: control scored 0/12 on rep 1, about half after; the sign flipped without it."""
    trials = [("control", "wrong", NOTHING), ("control", "insider_cluster", NOTHING)]
    by_rep = ex.analyse(ex.grade_rows(graded(tmp_path, trials), [Q]))["arms"]["control"]["by_rep"]
    assert by_rep == {1: [0, 1], 2: [1, 1]}


def test_answer_key_contact_removes_a_trial_from_the_clean_counts(tmp_path: Path) -> None:
    key = [{"pattern": "x", "path": "docs/eval/knowledge_questions.yaml"}]
    results = graded(tmp_path, [("control", "insider_cluster", key), ("control", "no", NOTHING)])
    arms = ex.analyse(ex.grade_rows(results, [Q]))["arms"]["control"]
    assert (
        arms["n"] == 2
        and arms["clean_n"] == 1
        and arms["correct"] == 1
        and arms["clean_correct"] == 0
    )


def test_the_analyse_command_can_store_the_grades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    results = graded(
        tmp_path, [("control", "no", NOTHING), ("treatment", "insider_cluster", NOTHING)]
    )
    monkeypatch.setattr(cli, "load", lambda *_a: type("F", (), {"tree": tmp_path})())
    monkeypatch.setattr(ex, "select_questions", lambda *_a: [Q])
    out = tmp_path / "graded.jsonl"
    assert ex.main(["analyse", str(results), "--write", str(out)]) == 0
    assert len(out.read_text().splitlines()) == 2 and "verdict" in capsys.readouterr().out
