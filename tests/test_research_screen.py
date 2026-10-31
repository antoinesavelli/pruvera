"""The research screen's runner and report, with a fake `run_trial` and synthetic trees only."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("yaml")

from bench import modelinfo, preflight, runner, stats  # noqa: E402
from bench.research import Question, grade, oracles, screen, screen_report  # noqa: E402
from tests.test_research_oracles import build  # noqa: E402

GIB = 2**30
SIZES = {"mid:7b": 5 * GIB, "big:70b": 40 * GIB, "small:1b": 1 * GIB}
Behave = Callable[[runner.TrialSpec], tuple[str, str]]


def tags_file(tmp_path: Path, sizes: dict[str, int] = SIZES) -> Path:
    path = tmp_path / "tags.json"
    path.write_text(json.dumps({"models": [{"name": n, "size": s} for n, s in sizes.items()]}))
    return path


def write_artifact(
    root: Path, name: str, text: str, tools: Sequence[str] = (), wrote: bool = False
) -> Path:
    adir = root / name
    adir.mkdir(parents=True)
    events = [{"type": "tool_use", "part": {"tool": "read", "state": {"input": {"filePath": t}}}}
              for t in tools]  # fmt: skip
    events.append({"type": "text", "part": {"text": text}})
    (adir / "transcript.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    (adir / "status.txt").write_text(" M pkg/x.py\n" if wrote else "")
    (adir / "diff.patch").write_text("")
    return adir


class Fake:
    """A stand-in for `runner.run_trial`: records the specs and appends synthetic rows."""

    def __init__(self, root: Path, behave: Behave | None = None, stop_after: int | None = None):
        self.root, self.specs, self.behave, self.stop_after = root, [], behave, stop_after

    def __call__(
        self, fx: Any, spec: runner.TrialSpec, artifacts: Path, trials: Path, out: Path, **kw: Any
    ) -> dict[str, Any]:
        if self.stop_after is not None and len(self.specs) >= self.stop_after:
            raise KeyboardInterrupt
        self.specs.append(spec)
        outcome, text = self.behave(spec) if self.behave else ("completed", "ANSWER: x")
        adir = write_artifact(self.root, spec.trial_id, text)
        rec = {
            "trial_id": spec.trial_id, "label": spec.label, "arm": spec.arm,
            "repeat": spec.repeat, "model": spec.model, "outcome": outcome, "secs": 10.0,
            "artifact": str(adir),
        }  # fmt: skip
        with out.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        return rec


@pytest.fixture(scope="module")
def tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build(tmp_path_factory.mktemp("tree"))


@pytest.fixture
def fake(tmp_path: Path, tree: Path, monkeypatch: pytest.MonkeyPatch) -> Fake:
    f = Fake(tmp_path / "artifacts")
    monkeypatch.setattr(runner, "run_trial", f)
    monkeypatch.setattr(runner, "load_profile", lambda *_a: SimpleNamespace(tree=tree))
    monkeypatch.setattr(preflight, "wait_clear", lambda *_a, **_k: [])
    monkeypatch.setattr(preflight, "session_lock", lambda: contextlib.nullcontext())
    return f


def cell_list(f: Fake) -> list[tuple[str, str, int]]:
    return [(s.arm, s.label, s.repeat) for s in f.specs]


# ---- run ---------------------------------------------------------------------------------------


def test_models_run_smallest_first_each_with_two_warmups_before_the_repeats(
    tmp_path: Path, fake: Fake
) -> None:
    out = tmp_path / "r.jsonl"
    screen.run(1, ["big:70b", "small:1b", "mid:7b"], 2, out, tags=tags_file(tmp_path))
    cells = cell_list(fake)
    assert len(cells) == 3 * (2 + 2 * 35)
    assert [m for m, _, _ in cells[::72]] == ["small:1b", "mid:7b", "big:70b"]
    first = cells[:72]
    assert first[:2] == [("small:1b", "warmup-1", 0), ("small:1b", "warmup-2", 0)]
    assert {r for _, _, r in first[2:]} == {1, 2}
    assert [r for _, _, r in first[2:]] == [1] * 35 + [2] * 35
    questions = {q.id for q in oracles.draw(fake_tree(fake), 1)}
    assert {lbl for _, lbl, r in first if r} == questions


def fake_tree(f: Fake) -> Path:
    return runner.load_profile("v", "clean").tree  # type: ignore[no-any-return]


def test_the_trial_spec_names_the_research_agent_the_model_and_the_cell(
    tmp_path: Path, fake: Fake
) -> None:
    screen.run(1, ["small:1b"], 1, tmp_path / "r.jsonl", tags=tags_file(tmp_path))
    spec = fake.specs[2]
    assert (spec.agent, spec.model, spec.arm, spec.repeat) == (
        "research",
        "small:1b",
        "small:1b",
        1,
    )
    assert (spec.timeout, spec.hang_seconds) == (300, 200)
    assert spec.prompt.endswith(grade.ANSWER_TAIL)
    assert spec.label in {q.id for q in oracles.draw(fake_tree(fake), 1)}


def test_a_rerun_skips_recorded_cells_and_warms_up_a_model_that_still_has_work(
    tmp_path: Path, fake: Fake
) -> None:
    out, tags = tmp_path / "r.jsonl", tags_file(tmp_path)
    fake.stop_after = 40
    with pytest.raises(KeyboardInterrupt):
        screen.run(1, ["small:1b", "mid:7b"], 1, out, tags=tags)
    done = cell_list(fake)
    fake.specs.clear()
    fake.stop_after = None
    screen.run(1, ["small:1b", "mid:7b"], 1, out, tags=tags)
    rest = cell_list(fake)
    assert len(done) == 40 and len(rest) == 2 + 34  # two fresh warm-ups, then the 34 left
    assert {m for m, _, _ in rest} == {"mid:7b"}
    assert [lbl for _, lbl, _ in rest[:2]] == ["warmup-1", "warmup-2"]
    assert not {c for c in rest[2:]} & set(done)
    assert screen.recorded(out) == set(done) | set(rest) and len(screen.recorded(out)) == 74
    fake.specs.clear()
    screen.run(1, ["small:1b", "mid:7b"], 1, out, tags=tags)
    assert fake.specs == []  # nothing left: no warm-up either


def test_harness_errors_and_lost_artifacts_are_retried_but_model_failures_are_kept(
    tmp_path: Path, fake: Fake
) -> None:
    out, tags = tmp_path / "r.jsonl", tags_file(tmp_path)
    calls: list[str] = []

    def behave(spec: runner.TrialSpec) -> tuple[str, str]:
        calls.append(spec.label)
        outcome = {2: "harness_error", 3: "timeout"}.get(len(calls) - 1, "completed")
        return outcome, "x"

    fake.behave = behave
    screen.run(1, ["small:1b"], 1, out, tags=tags)
    rows = screen.read_jsonl(out)
    shutil.rmtree(rows[4]["artifact"])  # a lost artifact is a lost cell too
    fake.specs.clear()
    fake.behave = None
    screen.run(1, ["small:1b"], 1, out, tags=tags)
    # the timeout (rows[3]) is model behaviour and stays recorded; the warm-ups run again
    assert sorted(s.label for s in fake.specs) == sorted(
        ["warmup-1", "warmup-2", rows[2]["label"], rows[4]["label"]]
    )
    assert len(screen.read_jsonl(out)) == 37 + 4


def test_disk_sizes_are_recorded_once_and_a_changed_size_warns_and_keeps_the_first(
    tmp_path: Path, fake: Fake, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "r.jsonl"
    screen.run(1, ["small:1b", "mid:7b"], 1, out, tags=tags_file(tmp_path))
    first = json.loads(screen.sizes_path(out).read_text())
    assert first == {"small:1b": GIB, "mid:7b": 5 * GIB}
    again = tags_file(tmp_path, {"small:1b": 9 * GIB, "mid:7b": 5 * GIB})
    screen.run(1, ["small:1b", "mid:7b"], 1, out, tags=again)
    assert "small:1b is now" in capsys.readouterr().out
    assert json.loads(screen.sizes_path(out).read_text()) == first
    assert screen.disk_gib(["small:1b"], out) == {"small:1b": 1.0}  # no tags needed any more


def test_the_report_uses_the_latest_row_of_a_retried_cell(tmp_path: Path) -> None:
    out = results_file(
        tmp_path,
        [trial("m", "L-a", outcome="harness_error"), trial("m", "L-a"), trial("m", "V-b")],
    )
    graded, excluded = screen_report.grade_rows(out, QS)
    assert excluded == [] and [r["question"] for r in graded] == ["L-a", "V-b"]
    assert all(r["exact"] for r in graded)


def test_other_questions_on_disk_refuse_the_run_before_any_trial(
    tmp_path: Path, fake: Fake
) -> None:
    out, tags = tmp_path / "r.jsonl", tags_file(tmp_path)
    screen.run(1, ["small:1b"], 1, out, tags=tags)
    fake.specs.clear()
    with pytest.raises(screen.Refusal, match="seed or the fixture"):
        screen.run(2, ["small:1b"], 1, out, tags=tags)
    assert fake.specs == []


def test_questions_are_written_with_their_keys_and_read_back_equal(
    tmp_path: Path, fake: Fake
) -> None:
    out = tmp_path / "r.jsonl"
    screen.run(1, ["small:1b"], 1, out, tags=tags_file(tmp_path))
    doc = json.loads(screen.questions_path(out).read_text())
    assert doc["seed"] == 1 and len(doc["questions"]) == 35
    assert screen.decode(doc) == oracles.draw(fake_tree(fake), 1)


def test_a_model_missing_from_the_tag_list_refuses(tmp_path: Path, fake: Fake) -> None:
    with pytest.raises(screen.Refusal, match="no disk size"):
        screen.run(1, ["small:1b", "ghost"], 1, tmp_path / "r.jsonl", tags=tags_file(tmp_path))
    assert fake.specs == []


def test_a_blocked_machine_stops_the_run_unless_forced(
    tmp_path: Path, fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preflight, "wait_clear", lambda *_a, **_k: [SimpleNamespace(code="gpu")])
    with pytest.raises(RuntimeError, match="gpu"):
        screen.run(1, ["small:1b"], 1, tmp_path / "r.jsonl", tags=tags_file(tmp_path))
    assert fake.specs == []
    screen.run(1, ["small:1b"], 1, tmp_path / "r.jsonl", force=True, tags=tags_file(tmp_path))
    assert len(fake.specs) == 37


def test_disk_sizes_come_from_the_tag_document_and_ignore_latest(tmp_path: Path) -> None:
    tags = tags_file(tmp_path, {"m:latest": 2 * GIB})
    assert screen.disk_gib(["m"], tmp_path / "none.jsonl", tags) == {"m": 2.0}


def test_model_sizes_reads_the_live_tag_list(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps({"models": [{"name": "m:latest", "size": 7}, {"name": "x:1b", "size": 3}]})
    monkeypatch.setattr(modelinfo, "_open", lambda *_a, **_k: io.BytesIO(body.encode()))
    assert modelinfo.model_sizes() == {"m": 7, "x:1b": 3}


# ---- dry run -----------------------------------------------------------------------------------


def test_dry_run_prints_pools_and_every_question_and_passes(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert screen.dry_run(tree, 1) == 0
    text = capsys.readouterr().out
    assert text.startswith("pool sizes: L=") and "disagreements: L=" in text
    assert "acceptance: ok" in text
    assert text.count("\tdecoys=") == 35


def test_dry_run_exits_non_zero_on_a_thin_pool(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("X = 1\n")
    assert screen.dry_run(tmp_path, 1) == 1
    assert "ACCEPTANCE FAILED" in capsys.readouterr().out


def test_dry_run_exits_non_zero_when_too_few_questions_or_a_source_is_missing(
    tree: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    drawn = oracles.draw(tree, 1)
    monkeypatch.setattr(oracles, "draw", lambda *_a, **_k: drawn[:30])
    assert screen.dry_run(tree, 1) == 1
    assert "30 questions drawn" in capsys.readouterr().out
    gone = Question("L-x", "L", "?", "path", "a.py", ("no/such/file.py",), ("b.py",))
    monkeypatch.setattr(oracles, "draw", lambda *_a, **_k: [*drawn[:34], gone])
    assert screen.dry_run(tree, 1) == 1
    assert "L-x: source no/such/file.py is not in the tree" in capsys.readouterr().out


def test_a_none_answer_may_name_a_directory_that_is_absent(tree: Path) -> None:
    none_q = Question("A-x", "A", "?", "set", frozenset(), ("nowhere/",), ("a.py",))
    assert screen.acceptance(tree, [none_q] * 35) == []


# ---- report ------------------------------------------------------------------------------------

QS = [
    Question("L-a", "L", "Where is a?", "path", "pkg/a.py", ("pkg/a.py",), ("pkg/b.py",)),
    Question("V-b", "V", "What is B?", "number", 7, ("pkg/c.py",), ()),
    Question("C-c", "C", "How many?", "number", 3, ("pkg",), ()),
    Question("I-d", "I", "Who imports d?", "set", frozenset({"t/x.py"}), ("t/x.py",), ()),
]
RIGHT = {"L-a": "ANSWER: pkg/a.py", "V-b": "ANSWER: 7", "C-c": "ANSWER: 3", "I-d": "ANSWER: t/x.py"}
SRC = {"L-a": "pkg/a.py", "V-b": "pkg/c.py", "C-c": "pkg", "I-d": "t/x.py"}


def results_file(tmp_path: Path, trials: list[dict[str, Any]]) -> Path:
    """Trial dicts as rows with artifacts (model, label, repeat, text, optional outcome etc.)."""
    out = tmp_path / "res.jsonl"
    lines = []
    for i, t in enumerate(trials):
        opened = [SRC[t["label"]]] if t.get("open", True) and t["label"] in SRC else []
        adir = write_artifact(tmp_path / "art", f"t{i}", t["text"], opened, t.get("wrote", False))
        lines.append(
            {"trial_id": f"t{i}", "label": t["label"], "arm": t["model"], "repeat": t["repeat"],
             "outcome": t.get("outcome", "completed"), "secs": t.get("secs", 10.0),
             "artifact": str(adir)}
        )  # fmt: skip
    out.write_text("".join(json.dumps(r) + "\n" for r in lines))
    return out


def trial(model: str, label: str, text: str | None = None, repeat: int = 1, **kw: Any) -> dict:
    return {
        "model": model,
        "label": label,
        "repeat": repeat,
        "text": RIGHT[label] if text is None else text,
        **kw,
    }


def test_grading_splits_failures_and_lists_harness_errors_apart(tmp_path: Path) -> None:
    out = results_file(
        tmp_path,
        [
            trial("m", "warmup-1", "ANSWER: junk", repeat=0),
            trial("m", "L-a"),
            trial("m", "V-b", "ANSWER: 8"),
            trial("m", "C-c", "It is 3, I think."),
            trial("m", "I-d", ""),
            trial("m", "L-a", repeat=2, outcome="timeout"),
            trial("m", "V-b", repeat=2, outcome="harness_error"),
            trial("m", "C-c", repeat=2, open=False),
            trial("m", "I-d", repeat=2, wrote=True),
        ],
    )
    graded, excluded = screen_report.grade_rows(out, QS)
    by = {(r["question"], r["repeat"]): r for r in graded}
    assert len(graded) == 6 and len(excluded) == 2
    assert [r["outcome"] for r in graded] == [
        "correct", "wrong", "no_answer_line", "no_answer", "correct", "correct",
    ]  # fmt: skip
    assert {(e["question"], e["repeat"]): e["reason"] for e in excluded} == {
        ("L-a", 2): "timeout",
        ("V-b", 2): "harness_error",
    }
    assert by[("C-c", 2)]["opened_source"] is False and by[("I-d", 2)]["wrote_files"] is True
    text = screen_report.markdown(graded, excluded, QS, {"m": 1.0}, "m", 0.75, 6)
    assert "| m | 1 | 1 | 1 | 1 | 1 | 0 | 2 |" in text.replace("  ", " ")  # failure split row
    assert "| m | L-a | 2 | timeout |" in text and "| m | V-b | 2 | harness_error |" in text
    assert "| m | 3/6 |" in text  # harness errors are not in n


def test_report_numbers_match_the_stats_helpers(tmp_path: Path) -> None:
    rows = [trial("m", q.id, None if i < 3 else "ANSWER: nope") for i, q in enumerate(QS)]
    out = results_file(tmp_path, rows)
    graded, excluded = screen_report.grade_rows(out, QS)
    text = screen_report.markdown(graded, excluded, QS, {"m": 1.0}, "m", 0.75, 6)
    low, high = stats.wilson(3, 4)
    assert f"| m | 3/4 | {low:.2f}-{high:.2f} |" in text
    rate, lo, hi = stats.cluster_ci({q.id: [i < 3] for i, q in enumerate(QS)})
    assert f"{rate:.2f} [{lo:.2f}, {hi:.2f}]" in text
    assert "| m | 1/1 | 1/1 | 1/1 | 0/1 | 0/0 | 0/0 | 0/0 |" in text  # per template


def graded_row(model: str, q: str, ok: bool, secs: float = 10.0, repeat: int = 1) -> dict[str, Any]:
    return {"model": model, "question": q, "template": q[0], "repeat": repeat, "seconds": secs,
            "outcome": "correct" if ok else "wrong", "exact": ok, "loose": ok,
            "opened_source": True, "contaminated": False, "wrote_files": False}  # fmt: skip


def rows_for(model: str, correct: int, total: int, secs: float = 10.0) -> list[dict[str, Any]]:
    return [graded_row(model, f"L-{i}", i < correct, secs) for i in range(total)]


def test_ranking_ties_go_to_the_smaller_model_then_lower_seconds_then_name() -> None:
    by_model = {
        "z-big": rows_for("z-big", 8, 10),
        "a-small": rows_for("a-small", 8, 10),
        "b-fast": rows_for("b-fast", 8, 10, 5.0),
        "c-top": rows_for("c-top", 9, 10),
        "d-same": rows_for("d-same", 8, 10, 5.0),
    }
    disk = {"z-big": 9.0, "a-small": 2.0, "b-fast": 2.0, "c-top": 30.0, "d-same": 2.0}
    got = screen_report.rank(by_model, disk, 10, 0.75, 6)
    assert [r.model for r in got] == ["c-top", "b-fast", "d-same", "a-small", "z-big"]


def test_models_below_the_drop_line_are_dropped_and_the_top_keep_go_to_round_2() -> None:
    by_model = {f"m{i}": rows_for(f"m{i}", 10 - i, 10) for i in range(5)}  # k = 10, 9, 8, 7, 6
    disk = {m: 1.0 for m in by_model}
    got = {r.model: r.status for r in screen_report.rank(by_model, disk, 10, 0.75, 2)}
    assert got == {"m0": "round 2", "m1": "round 2", "m2": "cut", "m3": "dropped", "m4": "dropped"}
    # ceil(0.75 x 10) = 8 correct is the lowest that survives
    assert got["m2"] != "dropped" and got["m3"] == "dropped"


def test_floor_and_ceiling_count_all_models_and_flag_when_over_half() -> None:
    rows = []
    for q, (a, b) in {"L-1": (1, 1), "L-2": (0, 0), "L-3": (1, 0), "L-4": (1, 1)}.items():
        rows += [graded_row("research", q, bool(a)), graded_row("other", q, bool(b))]
    assert screen_report.floor_ceiling(rows) == (1, 2, 4)
    qs = [Question(f"L-{i}", "L", "?", "path", "p", (), ()) for i in range(1, 5)]
    disk = {"research": 1.0, "other": 2.0}
    text = screen_report.markdown(rows, [], qs, disk, "research", 0.75, 6)
    # L-2 floor; L-1 and L-4 ceiling: 3 of 4 is more than half
    assert "1 floor (no model ever correct) and 2 ceiling" in text
    assert "**NON-DISCRIMINATING**" in text
    assert "alone (informational): 1 never correct and 3 always correct" in text


def test_at_one_repeat_the_flag_uses_all_models_not_the_research_model_alone() -> None:
    qs = [Question(f"L-{i}", "L", "?", "path", "p", (), ()) for i in range(1, 5)]
    right = {"research": [1, 1, 0, 0], "other": [0, 1, 1, 0]}
    rows = [
        graded_row(m, f"L-{i + 1}", bool(v)) for m, vs in right.items() for i, v in enumerate(vs)
    ]
    text = screen_report.markdown(
        rows, [], qs, {"research": 1.0, "other": 2.0}, "research", 0.75, 6
    )
    # the research model alone is all floor or ceiling (n = 1), but two questions split the models
    assert "1 floor (no model ever correct) and 1 ceiling" in text and "**discriminating**" in text
    assert "alone (informational): 2 never correct and 2 always correct" in text


def test_a_screen_with_half_its_questions_in_between_is_discriminating() -> None:
    qs = [Question(f"L-{i}", "L", "?", "path", "p", (), ()) for i in range(1, 5)]
    rows = [graded_row("r", "L-1", True), graded_row("r", "L-1", False, repeat=2)]
    rows += [graded_row("r", "L-2", True), graded_row("r", "L-2", False, repeat=2)]
    rows += [graded_row("r", "L-3", True), graded_row("r", "L-3", True, repeat=2)]
    rows += [graded_row("r", "L-4", False), graded_row("r", "L-4", False, repeat=2)]
    text = screen_report.markdown(rows, [], qs, {"r": 1.0}, "r", 0.75, 6)
    assert "1 floor (no model ever correct) and 1 ceiling" in text and "**discriminating**" in text


def test_a_model_with_every_trial_excluded_still_ranks_as_dropped_with_zero_correct() -> None:
    qs = [Question(f"L-{i}", "L", "?", "path", "p", (), ()) for i in range(1, 5)]
    rows = [graded_row("a", f"L-{i}", True) for i in range(1, 5)]
    lost = [{"model": "ghost", "question": "L-1", "repeat": 1, "reason": "harness_error"}]
    text = screen_report.markdown(rows, lost, qs, {"a": 1.0, "ghost": 0.5}, "a", 0.75, 6)
    assert "| 2 | ghost | 0/4 | 0.50 | - | dropped |" in text
    assert "| ghost | 0/4 |" in text  # the per-model table
    assert "| ghost | 0 | 0 | 0 | 0 | 0 | 0 | 1 |" in text  # the failure split


def test_a_research_model_without_graded_trials_is_not_evaluated() -> None:
    questions = [Question("L-1", "L", "?", "path", "p", (), ())]
    text = screen_report.markdown(
        [graded_row("a", "L-1", True)], [], questions, {"a": 1.0}, "gpt-oss", 0.75, 6
    )
    assert "has no graded trials: not evaluated" in text


def test_report_end_to_end_writes_one_graded_row_per_trial(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = results_file(
        tmp_path,
        [trial("small", "L-a"), trial("small", "V-b", "ANSWER: 1"), trial("big", "L-a")],
    )
    doc = screen.encode(1, QS)
    screen.questions_path(out).write_text(json.dumps(doc))
    tags = tags_file(tmp_path, {"small": GIB, "big": 9 * GIB})
    assert screen.main(["report", str(out), "--research-model", "small", "--sizes", str(tags)]) == 0
    text = capsys.readouterr().out
    assert "## Tournament ranking" in text and "| 1 | small | 1/2 |" in text
    rows = [json.loads(x) for x in screen.graded_path(out).read_text().splitlines()]
    assert len(rows) == 3
    assert set(rows[0]) == {
        "model", "question", "template", "repeat", "seconds", "outcome", "exact", "loose",
        "opened_source", "contaminated", "wrote_files",
    }  # fmt: skip
