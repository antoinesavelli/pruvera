"""The research screen's runner and report, with a fake `run_trial` and synthetic trees only."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("yaml")

from bench import experiment, modelinfo, preflight, registry, runner, stats  # noqa: E402
from bench.research import Question, grade, oracles, screen, screen_report  # noqa: E402
from tests.helpers import spec as _spec  # noqa: E402
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
            "artifact": str(adir), "fixture_profile": fx.profile,
            "experiment_id": spec.experiment_id,
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
    monkeypatch.setattr(runner, "load_profile", lambda _v, p: SimpleNamespace(tree=tree, profile=p))
    monkeypatch.setattr(runner, "check_fixture", lambda _fx: None)
    monkeypatch.setattr(runner, "require_agent_tools", lambda _argv: None)
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


def test_a_registered_run_checks_then_locks_then_authorizes_then_writes_then_runs(
    tmp_path: Path, fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    asked: list[tuple[str, dict[str, str], Any]] = []
    load, lock = runner.load_profile, preflight.session_lock
    questions, sizes = screen.write_questions, screen.record_sizes
    checks, raw, clear, drift = (
        screen._checks,
        screen.raw_sizes,
        preflight.wait_clear,
        runner.check_fixture,
    )

    def spy(version: str, profile: str) -> object:
        events.append(f"load {profile}")
        return load(version, profile)

    def authorize(experiment: str, profiles: dict[str, str], models: Any, *_a: Any) -> str:
        events.append("authorize")
        asked.append((experiment, profiles, models))
        return experiment

    @contextlib.contextmanager
    def locked() -> Any:
        events.append("lock")
        with lock():
            yield

    def noted(name: str, real: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(*a: Any, **k: Any) -> Any:
            events.append(name)
            return real(*a, **k)

        return wrapper

    monkeypatch.setattr(runner, "load_profile", spy)
    monkeypatch.setattr(registry, "authorize", authorize)
    monkeypatch.setattr(preflight, "session_lock", locked)
    monkeypatch.setattr(screen, "write_questions", noted("questions", questions))
    monkeypatch.setattr(screen, "record_sizes", noted("sizes", sizes))
    monkeypatch.setattr(screen, "_checks", noted("checks", checks))
    monkeypatch.setattr(screen, "raw_sizes", noted("raw_sizes", raw))
    monkeypatch.setattr(preflight, "wait_clear", noted("wait_clear", clear))
    monkeypatch.setattr(runner, "check_fixture", noted("check_fixture", drift))
    monkeypatch.setattr(runner, "require_caps", noted("require_caps", runner.require_caps))
    monkeypatch.setattr(
        runner, "require_agent_tools", noted("require_agent_tools", runner.require_agent_tools)
    )
    monkeypatch.setattr(runner, "run_trial", noted("trial", fake))
    out = tmp_path / "r.jsonl"
    screen.run(
        1, ["small:1b"], 1, out, tags=tags_file(tmp_path), profile="clean+v", experiment="exp-1"
    )
    assert events[:14] == [
        "load clean+v", "checks", "raw_sizes", "lock", "checks", "wait_clear", "check_fixture",
        "require_caps", "require_agent_tools", "authorize", "questions", "sizes", "wait_clear",
        "trial",
    ]  # fmt: skip
    assert asked == [("exp-1", {"baseline": "clean", "candidate": "clean+v"}, None)]
    assert fake.specs and {s.experiment_id for s in fake.specs} == {"exp-1"}


def test_a_variant_profile_without_a_registration_refuses_and_runs_nothing(
    tmp_path: Path, fake: Fake, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "r.jsonl"
    with pytest.raises(screen.Refusal, match="registered experiment"):
        screen.run(1, ["small:1b"], 1, out, tags=tags_file(tmp_path), profile="clean+v")
    assert fake.specs == [] and not out.exists() and not screen.questions_path(out).exists()
    argv = ["run", "--seed", "1", "--models", "small:1b", "--out", str(out)]
    assert screen.main([*argv, "--profile", "clean+v"]) == 2
    assert "refused:" in capsys.readouterr().err and fake.specs == []
    assert not list(tmp_path.glob("r*"))  # no results, questions or sizes file


def test_a_variant_profile_with_an_unknown_experiment_refuses(
    tmp_path: Path, fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(registry, "read", lambda *_a: [])
    with pytest.raises(screen.Refusal, match="not registered"):
        screen.run(
            1,
            ["small:1b"],
            1,
            tmp_path / "r",
            tags=tags_file(tmp_path),
            profile="clean+v",
            experiment="nope",
        )
    assert fake.specs == []


def test_plain_clean_needs_no_registration_and_its_trials_name_no_experiment(
    tmp_path: Path, fake: Fake
) -> None:
    screen.run(1, ["small:1b"], 1, tmp_path / "r.jsonl", tags=tags_file(tmp_path))
    assert fake.specs and {s.experiment_id for s in fake.specs} == {""}


# ---- the real registry, in a temporary repo ------------------------------------------------------


@pytest.fixture
def at(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`registry.authorize` over the temporary repo's registry, never the real one."""
    real = registry.authorize
    monkeypatch.setattr(
        registry, "authorize", lambda e, p, m, o, r=None, **k: real(e, p, m, o, root, **k)
    )
    experiment.register(_spec(root, "e-1", "exploratory", "clean", "clean+rule"), root)
    return root


def events_of(root: Path) -> list[str]:
    return [r["event"] for r in registry.read(root) if r.get("id") == "e-1"]


def screen_out(root: Path, name: str = "s.jsonl") -> Path:
    out = root / "results" / "research" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


def go(out: Path, tmp_path: Path, profile: str = "clean+rule", exp: str = "e-1") -> Path:
    return screen.run(
        1, ["small:1b"], 1, out, tags=tags_file(tmp_path), profile=profile, experiment=exp
    )


def test_a_real_registration_lets_the_screen_run_and_records_one_started_row(
    at: Path, fake: Fake, tmp_path: Path
) -> None:
    out = screen_out(at)
    go(out, tmp_path)
    assert fake.specs and {s.experiment_id for s in fake.specs} == {"e-1"}
    assert events_of(at) == ["registered", "started"]
    started = [r for r in registry.read(at) if r["event"] == "started"]
    assert started[0]["out"] == "results/research/s.jsonl"
    n = len(fake.specs)
    go(out, tmp_path)  # a resume
    assert len(fake.specs) == n and events_of(at) == ["registered", "started"]
    assert registry.verify(registry.read(at)) == []


def test_a_bad_model_after_a_registration_leaves_no_started_row_and_no_files(
    at: Path, fake: Fake, tmp_path: Path
) -> None:
    out = screen_out(at)
    with pytest.raises(screen.Refusal, match="no disk size"):
        screen.run(
            1, ["typo:1b"], 1, out, tags=tags_file(tmp_path), profile="clean+rule", experiment="e-1"
        )
    assert events_of(at) == ["registered"] and fake.specs == []
    assert list(out.parent.iterdir()) == []


def test_a_held_lock_leaves_no_started_row_and_no_files(
    at: Path, fake: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    @contextlib.contextmanager
    def held() -> Any:
        raise preflight.PreflightError("another run of trials holds the lock")
        yield

    monkeypatch.setattr(preflight, "session_lock", held)
    out = screen_out(at)
    with pytest.raises(preflight.PreflightError):
        go(out, tmp_path)
    assert events_of(at) == ["registered"] and fake.specs == []
    assert list(out.parent.iterdir()) == []


def test_questions_on_disk_for_another_seed_leave_no_started_row(
    at: Path, fake: Fake, tmp_path: Path
) -> None:
    out = screen_out(at)
    screen.run(2, ["small:1b"], 1, out, tags=tags_file(tmp_path))  # plain clean, seed 2
    with pytest.raises(screen.Refusal, match="other questions"):
        go(out, tmp_path)
    assert events_of(at) == ["registered"]


def test_a_variant_run_over_a_clean_results_file_refuses_before_any_start(
    at: Path, fake: Fake, tmp_path: Path
) -> None:
    out = screen_out(at)
    go(out, tmp_path, profile="clean", exp="")
    n = len(fake.specs)
    with pytest.raises(screen.Refusal, match="profile"):
        go(out, tmp_path)
    assert len(fake.specs) == n and events_of(at) == ["registered"]


def test_a_clean_run_over_a_variant_results_file_refuses(
    at: Path, fake: Fake, tmp_path: Path
) -> None:
    out = screen_out(at)
    go(out, tmp_path)
    n = len(fake.specs)
    with pytest.raises(screen.Refusal, match="profile"):
        go(out, tmp_path, profile="clean", exp="")
    assert len(fake.specs) == n


def test_a_plain_resume_still_runs_only_the_missing_cells(tmp_path: Path, fake: Fake) -> None:
    out = tmp_path / "r.jsonl"
    screen.run(1, ["small:1b"], 1, out, tags=tags_file(tmp_path))
    n = len(fake.specs)
    screen.run(1, ["small:1b"], 1, out, tags=tags_file(tmp_path))
    assert n and len(fake.specs) == n


def test_rows_without_a_profile_field_are_not_a_mismatch(tmp_path: Path) -> None:
    out = tmp_path / "r.jsonl"
    out.write_text(json.dumps({"arm": "m", "label": "q", "repeat": 1}) + "\n")
    screen.check_profile(out, "clean+rule")
    screen.check_profile(tmp_path / "missing.jsonl", "clean")


# ---- refusals before the study is marked started -------------------------------------------------


def only_registered(root: Path, out: Path) -> None:
    assert events_of(root) == ["registered"]
    assert not list(out.parent.glob("s.jsonl*"))


def test_main_forwards_the_experiment_to_the_run(at: Path, fake: Fake, tmp_path: Path) -> None:
    out = screen_out(at)
    argv = ["run", "--seed", "1", "--models", "small:1b", "--out", str(out)]
    argv += ["--sizes", str(tags_file(tmp_path)), "--profile", "clean+rule"]
    assert screen.main([*argv, "--experiment", "e-1"]) == 0
    assert {s.experiment_id for s in fake.specs} == {"e-1"}
    assert events_of(at) == ["registered", "started"]


def test_an_unregistered_variant_is_refused_before_the_profile_is_even_loaded(
    tmp_path: Path, fake: Fake, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing(_v: str, _p: str) -> object:
        raise FileNotFoundError("no manifest")

    monkeypatch.setattr(runner, "load_profile", missing)
    argv = ["run", "--seed", "1", "--models", "small:1b", "--out", str(tmp_path / "r.jsonl")]
    assert screen.main([*argv, "--profile", "clean+v"]) == 2
    assert "registered experiment" in capsys.readouterr().err


def test_a_profile_that_does_not_exist_is_a_refusal_not_a_traceback(
    at: Path, fake: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    def missing(_v: str, _p: str) -> object:
        raise FileNotFoundError("no manifest")

    monkeypatch.setattr(runner, "load_profile", missing)
    out = screen_out(at)
    argv = ["run", "--seed", "1", "--models", "small:1b", "--out", str(out)]
    argv += ["--sizes", str(tags_file(tmp_path)), "--profile", "clean+rule", "--experiment", "e-1"]
    assert screen.main(argv) == 2 and "no such profile" in capsys.readouterr().err
    only_registered(at, out)


@pytest.mark.parametrize("profile", ["clean+", "clean+@h", "tune+@x"])
def test_a_plus_profile_naming_no_variant_is_refused(
    at: Path, fake: Fake, tmp_path: Path, profile: str, capsys: pytest.CaptureFixture[str]
) -> None:
    out = screen_out(at)
    for exp in ("", "e-1"):
        argv = ["run", "--seed", "1", "--models", "small:1b", "--out", str(out)]
        argv += ["--sizes", str(tags_file(tmp_path)), "--profile", profile, "--experiment", exp]
        assert screen.main(argv) == 2 and "names no variant" in capsys.readouterr().err
    assert fake.specs == []
    only_registered(at, out)


@pytest.mark.parametrize("other", ["", "e-0"])
def test_a_registered_run_over_another_experiments_rows_refuses_before_any_start(
    at: Path, fake: Fake, tmp_path: Path, other: str
) -> None:
    out = screen_out(at)
    go(out, tmp_path)
    rows = [json.loads(x) for x in out.read_text().splitlines()]
    out.write_text("".join(json.dumps({**r, "experiment_id": other}) + "\n" for r in rows))
    n = len(fake.specs)
    with pytest.raises(screen.Refusal, match="experiment"):
        go(out, tmp_path)
    assert len(fake.specs) == n and events_of(at) == ["registered", "started"]


def test_a_plain_run_over_any_experiments_rows_is_unchanged(tmp_path: Path, fake: Fake) -> None:
    out = tmp_path / "r.jsonl"
    screen.run(1, ["small:1b"], 1, out, tags=tags_file(tmp_path))
    n = len(fake.specs)
    out.write_text(out.read_text().replace('"experiment_id": ""', '"experiment_id": "x"'))
    screen.run(1, ["small:1b"], 1, out, tags=tags_file(tmp_path))
    assert len(fake.specs) == n


@pytest.mark.parametrize(("models", "repeats"), [(["small:1b"], 0), ([], 1), ([""], 1)])
def test_an_empty_plan_is_refused_before_the_study_is_started(
    at: Path, fake: Fake, tmp_path: Path, models: list[str], repeats: int
) -> None:
    out = screen_out(at)
    with pytest.raises(screen.Refusal, match="nothing to run"):
        screen.run(
            1,
            models,
            repeats,
            out,
            tags=tags_file(tmp_path),
            profile="clean+rule",
            experiment="e-1",
        )
    only_registered(at, out)
    argv = ["run", "--seed", "1", "--models", ",", "--out", str(out), "--profile", "clean+rule"]
    assert screen.main([*argv, "--experiment", "e-1"]) == 2
    only_registered(at, out)


def test_a_blocked_machine_at_the_first_trial_leaves_no_started_row(
    at: Path, fake: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preflight, "wait_clear", lambda *_a, **_k: [SimpleNamespace(code="gpu")])
    out = screen_out(at)
    with pytest.raises(RuntimeError, match="gpu"):
        go(out, tmp_path)
    only_registered(at, out)
    assert fake.specs == []


def test_a_drifted_fixture_leaves_no_started_row(
    at: Path, fake: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def drifted(_fx: object) -> None:
        raise runner.DriftError("base drifted")

    monkeypatch.setattr(runner, "check_fixture", drifted)
    out = screen_out(at)
    with pytest.raises(runner.DriftError):
        go(out, tmp_path)
    only_registered(at, out)


def test_the_checks_are_repeated_under_the_lock_against_a_racing_session(
    at: Path, fake: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = screen_out(at)

    @contextlib.contextmanager
    def racing() -> Any:
        out.write_text(json.dumps({"fixture_profile": "clean+rule", "experiment_id": ""}) + "\n")
        yield

    monkeypatch.setattr(preflight, "session_lock", racing)
    with pytest.raises(screen.Refusal, match="experiment"):
        go(out, tmp_path)
    assert events_of(at) == ["registered"] and fake.specs == []


def test_the_model_check_comes_before_the_lock(
    at: Path, fake: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    @contextlib.contextmanager
    def held() -> Any:
        raise preflight.PreflightError("another run of trials holds the lock")
        yield

    monkeypatch.setattr(preflight, "session_lock", held)
    with pytest.raises(screen.Refusal, match="no disk size"):
        screen.run(
            1, ["typo:1b"], 1, screen_out(at), tags=tags_file(tmp_path),
            profile="clean+rule", experiment="e-1",
        )  # fmt: skip


def test_an_uncreatable_out_or_a_corrupt_sizes_file_leaves_no_started_row(
    at: Path, fake: Fake, tmp_path: Path
) -> None:
    locked = tmp_path / "ro"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        if os.access(locked, os.W_OK):  # running as root: permissions cannot refuse
            pytest.skip("directory permissions do not bind this user")
        with pytest.raises(screen.Refusal, match="cannot write"):
            go(locked / "deep" / "s.jsonl", tmp_path)
    finally:
        locked.chmod(0o700)
    out = screen_out(at)
    screen.sizes_path(out).write_text("{not json")
    with pytest.raises(screen.Refusal, match="corrupt"):
        go(out, tmp_path)
    assert events_of(at) == ["registered"] and fake.specs == []
    assert [p.name for p in out.parent.iterdir()] == [screen.sizes_path(out).name]


@pytest.mark.parametrize("which", ["out", "questions", "sizes"])
def test_a_read_only_existing_file_is_refused_with_no_started_row(
    at: Path, fake: Fake, tmp_path: Path, which: str, capsys: pytest.CaptureFixture[str]
) -> None:
    out = screen_out(at)
    path = {"out": out, "questions": screen.questions_path(out), "sizes": screen.sizes_path(out)}[
        which
    ]
    seed = tmp_path / "seed.jsonl"  # valid questions and sizes files, from a plain clean run
    screen.run(1, ["small:1b"], 1, seed, tags=tags_file(tmp_path))
    n = len(fake.specs)
    shutil.copy(screen.questions_path(seed), screen.questions_path(out))
    shutil.copy(screen.sizes_path(seed), screen.sizes_path(out))
    out.write_text("")
    path.chmod(0o400)
    try:
        if os.access(path, os.W_OK):  # running as root: permissions cannot refuse
            pytest.skip("file permissions do not bind this user")
        argv = ["run", "--seed", "1", "--models", "small:1b", "--out", str(out)]
        argv += ["--sizes", str(tags_file(tmp_path)), "--profile", "clean+rule"]
        assert screen.main([*argv, "--experiment", "e-1"]) == 2
        assert "is not writable" in capsys.readouterr().err
    finally:
        path.chmod(0o600)
    assert events_of(at) == ["registered"] and len(fake.specs) == n
