"""Self-test of the research-screen grader on synthetic questions: every kind, every failure."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("yaml")

from bench.research import Question  # noqa: E402
from bench.research import grade as g  # noqa: E402
from bench.sandbox import WORKDIR  # noqa: E402
from bench.transcript import answer_kind  # noqa: E402

PATH_Q = Question("L-foo", "L", "Where is foo?", "path", "pkg/foo.py", ("pkg/foo.py",), ())
SET_Q = Question(
    "I-bar", "I", "Who imports bar?", "set", frozenset({"tests/a.py", "tests/b.py"}), (), ()
)
NONE_Q = Question("A-baz", "A", "Any importer of baz?", "set", frozenset(), ("pkg/",), ())
NUM_Q = Question("C-dir", "C", "How many files?", "number", 12, ("pkg",), ())
STR_Q = Question("V-limit", "V", "What is LIMIT?", "string", "fast", ("pkg/c.py",), ())
FLOAT_Q = Question("V-ratio", "V", "What is RATIO?", "number", 5.0, ("pkg/c.py",), ())


def run(q: Question, text: str, tools: list[dict[str, Any]] | None = None, wrote: bool = False):
    return g.grade(q, text, answer_kind(text), tools or [], wrote)


def tool(**inputs: Any) -> dict[str, Any]:
    return {"tool": "read", "state": {"input": inputs}}


@pytest.mark.parametrize(
    ("q", "line"),
    [
        (PATH_Q, "ANSWER: pkg/foo.py"),
        (SET_Q, "ANSWER: tests/a.py, tests/b.py"),
        (NONE_Q, "ANSWER: none"),
        (NUM_Q, "ANSWER: 12"),
        (STR_Q, "ANSWER: fast"),
        (FLOAT_Q, "ANSWER: 5"),
    ],
)
def test_model_answer_is_correct(q: Question, line: str) -> None:
    got = run(q, f"I looked it up.\n{line}")
    assert (got.outcome, got.exact, got.loose) == ("correct", True, True)


@pytest.mark.parametrize("q", [PATH_Q, SET_Q, NONE_Q, NUM_Q, STR_Q])
@pytest.mark.parametrize("text", ["", "   \n", '{"name": "read", "arguments": {}}'])
def test_idle_or_tool_json_is_no_answer(q: Question, text: str) -> None:
    got = run(q, text)
    assert (got.outcome, got.exact, got.loose, got.answer) == ("no_answer", False, False, "")


@pytest.mark.parametrize(
    ("q", "line"),
    [
        (PATH_Q, "ANSWER: pkg/other.py"),
        (PATH_Q, "ANSWER: tests/test_foo.py"),  # decoy: a caller
        (SET_Q, "ANSWER: tests/a.py"),
        (SET_Q, "ANSWER: tests/a.py, tests/b.py, tests/c.py"),
        (SET_Q, "ANSWER: none"),
        (NONE_Q, "ANSWER: pkg/x.py"),
        (NUM_Q, "ANSWER: 13"),  # decoy: with __init__.py
        (NUM_Q, "ANSWER: 11"),
        (NUM_Q, "ANSWER: 12 (13 with __init__.py)"),
        (STR_Q, "ANSWER: slow"),
    ],
)
def test_wrong_value_is_wrong(q: Question, line: str) -> None:
    got = run(q, f"text\n{line}")
    assert (got.outcome, got.exact) == ("wrong", False)


def test_fact_in_prose_without_answer_line() -> None:
    got = run(PATH_Q, "The function is defined in pkg/foo.py, near the top.")
    assert (got.outcome, got.exact, got.loose, got.answer) == ("no_answer_line", False, True, "")
    assert run(NUM_Q, "There are 12 files.").loose
    assert not run(NUM_Q, "There are 13 files.").loose
    assert run(NONE_Q, "Nothing imports it: none.").loose
    assert run(SET_Q, "tests/b.py and tests/a.py import it").loose
    assert not run(SET_Q, "tests/a.py imports it").loose


def test_loose_does_not_match_inside_a_longer_path_or_number() -> None:
    assert not run(PATH_Q, "see other/pkg/foo.py and pkg/foo.pyc").loose
    assert not run(NUM_Q, "version 1.12 and 120 files").loose
    assert run(PATH_Q, f"It is {WORKDIR}/pkg/foo.py.").loose


def test_wrong_line_with_fact_in_prose_is_wrong_but_loose() -> None:
    got = run(PATH_Q, "It is in pkg/foo.py really.\nANSWER: pkg/bar.py")
    assert (got.outcome, got.loose) == ("wrong", True)


@pytest.mark.parametrize(
    ("q", "line"),
    [
        (PATH_Q, "ANSWER: `pkg/foo.py`"),
        (PATH_Q, "answer: pkg/foo.py"),
        (PATH_Q, "ANSWER: pkg/foo.py."),
        (PATH_Q, "ANSWER: ./pkg/foo.py"),
        (PATH_Q, f"ANSWER: {WORKDIR}/pkg/foo.py"),
        (PATH_Q, f"ANSWER: `{WORKDIR}/pkg/foo.py`."),
        (PATH_Q, "**ANSWER:** pkg/foo.py"),
        (SET_Q, "ANSWER: tests/b.py, tests/a.py"),
        (SET_Q, "ANSWER: tests/a.py and tests/b.py."),
        (SET_Q, "ANSWER: `tests/a.py`; `tests/b.py`"),
        (SET_Q, f"ANSWER: ./tests/a.py {WORKDIR}/tests/b.py"),
        (NONE_Q, "ANSWER: None."),
        (NUM_Q, "ANSWER: `12`."),
        (NUM_Q, "ANSWER: 12.0"),
        (STR_Q, 'ANSWER: "fast"'),
        (FLOAT_Q, "ANSWER: 5.0"),
    ],
)
def test_format_variants_are_correct(q: Question, line: str) -> None:
    assert run(q, f"Found.\n{line}\n").outcome == "correct"


def test_last_answer_line_wins() -> None:
    assert run(PATH_Q, "ANSWER: pkg/other.py\nOn reflection:\nANSWER: pkg/foo.py").exact
    assert not run(PATH_Q, "ANSWER: pkg/foo.py\nActually:\nANSWER: pkg/other.py").exact


@pytest.mark.parametrize(
    "line",
    [
        "**ANSWER**: `pkg/foo.py`",
        "**Answer**: pkg/foo.py",
        "## ANSWER: pkg/foo.py",
        "1. ANSWER: pkg/foo.py",
        "> **ANSWER:** pkg/foo.py",
        "- Answer : pkg/foo.py",
    ],
)
def test_markdown_around_the_answer_word(line: str) -> None:
    assert run(PATH_Q, f"Found.\n{line}").exact


def test_empty_answer_line_is_not_an_answer_line() -> None:
    assert run(PATH_Q, "text\nANSWER:").outcome == "no_answer_line"
    assert run(SET_Q, "text\nANSWER: \n**ANSWER:**").outcome == "no_answer_line"
    assert run(PATH_Q, "ANSWER: pkg/foo.py\nANSWER:").exact  # falls back to the previous line


def test_and_splits_a_set_only_between_spaces() -> None:
    q = Question("I-and", "I", "?", "set", frozenset({"and.py", "and/x.py", "b.py"}), (), ())
    assert run(q, "ANSWER: and.py, and/x.py and b.py").exact
    assert run(q, "ANSWER: and.py, and b.py, and/x.py").exact
    assert not run(q, "ANSWER: and.py, b.py").exact


def test_numbers_inside_paths_do_not_count() -> None:
    assert run(NUM_Q, "ANSWER: 12 (in tests/v2/test_a1.py)").exact
    assert run(NUM_Q, "ANSWER: 12.").exact
    assert not run(NUM_Q, "ANSWER: 12 (13 with __init__.py)").exact
    half = Question("V-half", "V", "?", "number", 0.5, (), ())
    assert run(half, "ANSWER: .5").exact
    assert run(half, "ANSWER: 0.5").exact


def test_opened_source_needs_a_path_boundary() -> None:
    q = Question("L-a", "L", "?", "path", "utils/a.py", ("utils/a.py",), ())
    ok = [
        tool(filePath="utils/a.py"),
        tool(filePath=f"{WORKDIR}/utils/a.py"),
        tool(filePath="./utils/a.py"),
        tool(command="cat utils/a.py | head"),
        tool(command="rg foo --glob=utils/a.py"),
    ]
    for t in ok:
        assert run(q, "ANSWER: utils/a.py", [t]).opened_source, t
    for bad in (tool(filePath="x/utils/a.py"), tool(filePath="utils/a.pyc"), tool(filePath="u")):
        assert not run(q, "ANSWER: utils/a.py", [bad]).opened_source, bad


@pytest.mark.parametrize("status", ["0 lines omitted\n", "5 lines omitted\n", "!! a/\n", ""])
def test_status_placeholders_and_ignored_are_not_changes(tmp_path: Path, status: str) -> None:
    art = make_artifact(tmp_path, "ANSWER: pkg/foo.py", status=status)
    assert not g.grade_artifact(PATH_Q, art).wrote_files


def test_answer_must_start_a_line() -> None:
    got = run(PATH_Q, "I will end with ANSWER: pkg/foo.py when sure.")
    assert got.outcome == "no_answer_line"


def test_answer_is_the_normalised_value() -> None:
    assert run(PATH_Q, f"ANSWER: `{WORKDIR}/pkg/foo.py`.").answer == "pkg/foo.py"


def test_opened_source_and_contaminated() -> None:
    plain = run(PATH_Q, "ANSWER: pkg/foo.py", [tool(filePath=f"{WORKDIR}/pkg/foo.py")])
    assert plain.opened_source and not plain.contaminated
    guess = run(PATH_Q, "ANSWER: pkg/foo.py", [tool(pattern="foo"), tool(command="ls")])
    assert not guess.opened_source
    assert not run(PATH_Q, "ANSWER: pkg/foo.py").opened_source
    dirty = run(PATH_Q, "ANSWER: pkg/foo.py", [tool(filePath="docs/eval/knowledge_questions.yaml")])
    assert dirty.contaminated and dirty.exact
    assert not run(PATH_Q, "ANSWER: pkg/foo.py", [{"tool": "read"}]).opened_source


def test_wrote_files_is_passed_through() -> None:
    assert run(PATH_Q, "ANSWER: pkg/foo.py", wrote=True).wrote_files
    assert not run(PATH_Q, "ANSWER: pkg/foo.py").wrote_files


def test_prompt_tail_states_the_format() -> None:
    assert "ANSWER: <value>" in g.ANSWER_TAIL
    assert "ANSWER: none" in g.ANSWER_TAIL
    assert "format requirement" in g.ANSWER_TAIL
    assert "modify" in g.ANSWER_TAIL


def make_artifact(tmp: Path, text: str, status: str = "", diff: str = "") -> Path:
    art = tmp / "art"
    art.mkdir()
    events = [
        {"type": "tool_use", "part": tool(filePath="pkg/foo.py")},
        {"type": "text", "part": {"text": text}},
    ]
    (art / "transcript.jsonl").write_text("\n".join(json.dumps(e) for e in events))
    (art / "status.txt").write_text(status)
    (art / "diff.patch").write_text(diff)
    return art


def test_grade_artifact_reads_transcript_and_repo_state(tmp_path: Path) -> None:
    art = make_artifact(tmp_path, "ANSWER: pkg/foo.py", status="!! pkg/__pycache__/\n")
    got = g.grade_artifact(PATH_Q, art)
    assert (got.outcome, got.opened_source, got.wrote_files) == ("correct", True, False)


@pytest.mark.parametrize(
    ("status", "diff"), [("?? notes.txt\n", ""), (" M pkg/foo.py\n", "diff --git a b\n"), ("", "x")]
)
def test_grade_artifact_flags_changes(tmp_path: Path, status: str, diff: str) -> None:
    art = make_artifact(tmp_path, "ANSWER: pkg/foo.py", status, diff)
    assert g.grade_artifact(PATH_Q, art).wrote_files
    assert not g.grade_artifact(PATH_Q, art, wrote_files=False).wrote_files


def test_markdown_rules_do_not_hang_the_answer_line_regex() -> None:
    for rule in ("-" * 200, "*" * 200, "- " * 100, "1." * 100):
        start = time.perf_counter()
        got = run(PATH_Q, f"{rule}\n{rule}\nANSWER: pkg/foo.py")
        assert got.exact
        assert time.perf_counter() - start < 0.1


def test_opened_source_ignores_tool_output() -> None:
    q = Question("L-a", "L", "?", "path", "utils/a.py", ("utils/a.py",), ())
    listing = {
        "tool": "glob",
        "state": {
            "input": {"pattern": "**/*.py"},
            "output": "utils/a.py\nutils/b.py",
            "title": "utils/a.py",
            "metadata": {"files": ["utils/a.py"]},
        },
    }
    assert not run(q, "ANSWER: utils/a.py", [listing]).opened_source


@pytest.mark.parametrize("line", ["ANSWER: 3-5", "ANSWER: 12-13", "ANSWER: 12-13."])
def test_ranges_are_two_numbers(line: str) -> None:
    assert run(NUM_Q, line).outcome == "wrong"
    three = Question("C-3", "C", "?", "number", 3, (), ())
    assert run(three, "ANSWER: 3-5").outcome == "wrong"
    assert run(three, "ANSWER: -3").outcome == "wrong"
