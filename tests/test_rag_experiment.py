"""Tests for the retrieval A/B: question selection, grading, transcripts, the verdict rule."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("yaml")

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


def stats(n: int, correct: int, errors: int) -> dict[str, int]:
    return {
        "n": n,
        "correct": correct,
        "clean_n": n,
        "clean_correct": correct,
        "tool_errors": errors,
        "searches": 0,
    }


def test_verdict_follows_the_pre_registered_rule() -> None:
    assert ex.verdict({"control": stats(36, 20, 2), "treatment": stats(36, 27, 2)}).startswith(
        "ADOPT"
    )
    assert ex.verdict({"control": stats(36, 20, 2), "treatment": stats(36, 22, 2)}).startswith(
        "DO NOT"
    )
    assert ex.verdict({"control": stats(36, 20, 2), "treatment": stats(36, 30, 9)}).startswith(
        "DO NOT"
    )
    assert "not evaluable" in ex.verdict({"control": stats(36, 20, 2)})


def test_analyse_grades_trials_from_their_artifacts(tmp_path: Path) -> None:
    q = ex.Question("ok", "?", ("insider_cluster",), ())
    rows = []
    for arm, answer in (("control", "no idea"), ("treatment", "insider_cluster")):
        art = make_artifact(tmp_path, arm, answer, [{"pattern": "x"}])
        rows.append({"label": "ok", "arm": arm, "artifact": str(art), "tool_errors": 0})
    results = tmp_path / "r.jsonl"
    results.write_text("\n".join(json.dumps(r) for r in rows))
    out = ex.analyse(results, [q])
    assert out["arms"]["control"]["correct"] == 0 and out["arms"]["treatment"]["correct"] == 1
    assert out["verdict"].startswith("ADOPT")
