"""The open-ended research runner (M9): run, export-answers and report, on synthetic questions and a
synthetic tree, with a fake `run_trial` and no model, Ollama or fixture."""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import json
import os
import re
from collections.abc import Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("yaml")

from bench import preflight, registry, runner, stats  # noqa: E402
from bench.research import open_grade, open_judge, open_questions, open_screen, screen  # noqa: E402

GIB = 2**30
SMALL, MID, BIG = "qwen3.6:27b-64k", "gemma4:26b-64k", "laguna-xs-2.1:q4_K_M-64k"
SIZES = {SMALL: 17 * GIB, MID: 9 * GIB, BIG: 30 * GIB}
SEED = 41
TEXT = {"T-1": "How does the alpha helper reach beta?", "N-1": "Is there a gamma cache?",
        "E-1": "Explain how delta is configured."}  # fmt: skip
FACT = {"t1": "alpha calls beta in mod.py", "t2": "beta returns one", "n1": "no gamma cache exists",
        "e1": "delta comes from the config", "e2": "delta defaults to two"}  # fmt: skip
CLAIM = {
    "c1": "alpha writes to disk",
    "m1": "gamma is cached in memory",
    "x1": "delta is hard coded",
}
MODULE = "def alpha():\n    return beta()\n\n\ndef beta():\n    return 1\n"


def question(
    qid: str, kind: str, required: list[str], forbidden: str, no_cite: bool
) -> dict[str, Any]:
    anchor = {"path": "pkg/mod.py", "line": 1, "text": "def alpha"}
    return {
        "id": qid,
        "kind": kind,
        "text": TEXT[qid],
        "required": [{"id": f, "text": FACT[f], "anchors": [anchor]} for f in required],
        "forbidden": [{"id": forbidden, "text": CLAIM[forbidden]}],
        "no_citation_ok": no_cite,
    }


def doc(
    split: str = "dev", seed: int = SEED, ids: Sequence[str] = ("T-1", "N-1", "E-1")
) -> dict[str, Any]:
    every = {
        "T-1": question("T-1", "Trace", ["t1", "t2"], "c1", False),
        "N-1": question("N-1", "Negative", ["n1"], "m1", True),
        "E-1": question("E-1", "Explain", ["e1", "e2"], "x1", False),
    }
    return {"split": split, "seed": seed, "questions": [every[i] for i in ids]}


@pytest.fixture
def qdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A question directory with a dev and a screen file, standing in for the fixture's."""
    where = tmp_path / "research_open"
    where.mkdir()
    for split in ("dev", "screen"):
        (where / f"{split}.json").write_text(json.dumps(doc(split)))
    monkeypatch.setattr(open_screen, "question_file", lambda s: where / f"{s}.json")
    return where


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text(MODULE)
    return root


def tags_file(tmp_path: Path, sizes: dict[str, int] = SIZES) -> Path:
    path = tmp_path / "tags.json"
    path.write_text(json.dumps({"models": [{"name": n, "size": s} for n, s in sizes.items()]}))
    return path


def write_artifact(
    root: Path, name: str, text: str, inputs: Sequence[int] = (), bodies: Sequence[int] = ()
) -> Path:
    adir = root / name
    adir.mkdir(parents=True)
    events: list[dict[str, Any]] = [
        {"type": "step_finish", "part": {"tokens": {"input": n, "output": 1}}} for n in inputs
    ]
    events.append({"type": "text", "part": {"text": text}})
    (adir / "transcript.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    if bodies:
        (adir / "ollama_trace.jsonl").write_text(
            "".join(json.dumps({"path": "/x", "body_len": b}) + "\n" for b in bodies)
        )
    return adir


class Fake:
    """A stand-in for `runner.run_trial`: records the specs and appends synthetic rows."""

    def __init__(self, root: Path, stop_after: int | None = None) -> None:
        self.root, self.specs, self.stop_after = root, [], stop_after
        self.outcome: Callable[[runner.TrialSpec], str] = lambda _s: "completed"

    def __call__(
        self, fx: Any, spec: runner.TrialSpec, artifacts: Path, trials: Path, out: Path, **kw: Any
    ) -> dict[str, Any]:
        if self.stop_after is not None and len(self.specs) >= self.stop_after:
            raise KeyboardInterrupt
        self.specs.append(spec)
        adir = write_artifact(self.root, spec.trial_id, "an answer")
        rec = {
            "trial_id": spec.trial_id, "label": spec.label, "arm": spec.arm, "repeat": spec.repeat,
            "model": spec.model, "outcome": self.outcome(spec), "secs": 10.0, "artifact": str(adir),
            "fixture_profile": fx.profile, "experiment_id": spec.experiment_id,
        }  # fmt: skip
        with out.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        return rec


@pytest.fixture
def fake(tmp_path: Path, tree: Path, monkeypatch: pytest.MonkeyPatch, qdir: Path) -> Fake:
    f = Fake(tmp_path / "artifacts")
    monkeypatch.setattr(runner, "run_trial", f)
    monkeypatch.setattr(runner, "load_profile", lambda _v, p: SimpleNamespace(tree=tree, profile=p))
    monkeypatch.setattr(runner, "check_fixture", lambda _fx: None)
    monkeypatch.setattr(runner, "require_agent_tools", lambda _argv: None)
    monkeypatch.setattr(preflight, "wait_clear", lambda *_a, **_k: [])
    monkeypatch.setattr(preflight, "session_lock", lambda: contextlib.nullcontext())
    return f


def cells(f: Fake) -> list[tuple[str, str, int]]:
    return [(s.arm, s.label, s.repeat) for s in f.specs]


def go(
    tmp_path: Path, out: Path | None = None, models: Sequence[str] = (SMALL,), **kw: Any
) -> Path:
    kw.setdefault("tags", tags_file(tmp_path))
    return open_screen.run(
        "dev", list(models), kw.pop("repeats", 1), out or tmp_path / "r.jsonl", **kw
    )


# ---- run: order, prompts, limits ---------------------------------------------------------------


def test_models_run_smallest_on_disk_first_each_with_two_warmups_and_colon_tags_resolve(
    tmp_path: Path, fake: Fake
) -> None:
    go(tmp_path, models=[BIG, SMALL, MID], repeats=2)
    got = cells(fake)
    assert len(got) == 3 * (2 + 2 * 3)
    assert [m for m, _, _ in got[::8]] == [MID, SMALL, BIG]  # 9, 17 and 30 GiB
    first = got[:8]
    assert first[:2] == [(MID, "warmup-1", 0), (MID, "warmup-2", 0)]
    assert [r for _, _, r in first[2:]] == [1, 1, 1, 2, 2, 2]
    assert {lbl for _, lbl, r in first if r} == set(TEXT)
    assert json.loads(screen.sizes_path(tmp_path / "r.jsonl").read_text())[BIG] == 30 * GIB


def test_the_prompt_is_the_question_text_as_written_with_no_format_tail(
    tmp_path: Path, fake: Fake
) -> None:
    go(tmp_path)
    assert {s.prompt for s in fake.specs if s.repeat} == set(TEXT.values())
    warm = [s for s in fake.specs if not s.repeat]
    assert [s.prompt for s in warm] == [TEXT["T-1"], TEXT["N-1"]]  # the first two, as written
    assert all(TEXT[s.label] == s.prompt for s in fake.specs if s.repeat)


def test_the_trial_spec_names_the_research_agent_and_allows_600_seconds(
    tmp_path: Path, fake: Fake
) -> None:
    go(tmp_path)
    spec = fake.specs[2]
    assert (spec.agent, spec.model, spec.arm, spec.repeat) == ("research", SMALL, SMALL, 1)
    assert (spec.timeout, spec.hang_seconds) == (600, 400)
    assert spec.seed is None and spec.experiment_id == ""
    assert open_screen.trial_spec((SMALL, "T-1", 1), "p", "e-9").experiment_id == "e-9"


def test_each_repeat_shuffles_the_questions_by_the_seed_as_the_screen_does(
    tmp_path: Path, fake: Fake
) -> None:
    go(tmp_path, repeats=3)
    qs = open_questions.load(screen.questions_path(tmp_path / "r.jsonl")).questions
    for r in (1, 2, 3):
        got = [lbl for _, lbl, rep in cells(fake) if rep == r]
        assert got == [q.id for q in open_screen._order(qs, SEED, r)]
    assert len({tuple(lbl for _, lbl, rep in cells(fake) if rep == r) for r in (1, 2, 3)}) > 1
    other = [q.id for q in open_screen._order(qs, SEED + 1, 1)]
    assert sorted(other) == sorted(TEXT)


# ---- run: resume -------------------------------------------------------------------------------


def test_a_rerun_skips_recorded_cells_and_warms_up_only_a_model_with_work_left(
    tmp_path: Path, fake: Fake
) -> None:
    out = tmp_path / "r.jsonl"
    fake.stop_after = 4 + 2  # MID's two warm-ups and three cells, then two of SMALL's warm-up
    with pytest.raises(KeyboardInterrupt):
        go(tmp_path, out, [SMALL, MID])
    done = cells(fake)
    fake.specs.clear()
    fake.stop_after = None
    go(tmp_path, out, [SMALL, MID])
    rest = cells(fake)
    assert not {c for c in rest if c[2]} & set(done) and {m for m, _, _ in rest} == {SMALL}
    assert [lbl for _, lbl, _ in rest[:2]] == ["warmup-1", "warmup-2"] and len(rest) == 2 + 3
    fake.specs.clear()
    go(tmp_path, out, [SMALL, MID])
    assert fake.specs == []  # nothing left: not even a warm-up


def test_a_harness_error_is_retried_and_a_timeout_is_kept(tmp_path: Path, fake: Fake) -> None:
    out = tmp_path / "r.jsonl"
    fake.outcome = lambda s: {"T-1": "harness_error", "N-1": "timeout"}.get(s.label, "completed")
    go(tmp_path, out)
    fake.specs.clear()
    fake.outcome = lambda _s: "completed"
    go(tmp_path, out)
    assert sorted(s.label for s in fake.specs) == ["T-1", "warmup-1", "warmup-2"]


# ---- run: guards -------------------------------------------------------------------------------


def test_the_guards_run_in_the_screens_order_and_the_lock_comes_before_the_registration(
    tmp_path: Path, fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    asked: list[tuple[str, dict[str, str], Any]] = []
    real = {
        "lock": preflight.session_lock,
        "bound": open_screen.write_bound,
        "sizes": screen.record_sizes,
        "checks": open_screen._checks,
        "raw": screen.raw_sizes,
        "load": open_screen.load_split,
    }

    def authorize(experiment: str, profiles: dict[str, str], models: Any, *_a: Any) -> str:
        events.append("authorize")
        asked.append((experiment, profiles, models))
        return experiment

    @contextlib.contextmanager
    def locked() -> Any:
        events.append("lock")
        with real["lock"]():
            yield

    def noted(name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(*a: Any, **k: Any) -> Any:
            events.append(name)
            return fn(*a, **k)

        return wrapper

    load = runner.load_profile
    monkeypatch.setattr(runner, "load_profile", noted("profile", load))
    monkeypatch.setattr(registry, "authorize", authorize)
    monkeypatch.setattr(preflight, "session_lock", locked)
    monkeypatch.setattr(open_screen, "write_bound", noted("bound", real["bound"]))
    monkeypatch.setattr(screen, "record_sizes", noted("sizes", real["sizes"]))
    monkeypatch.setattr(open_screen, "_checks", noted("checks", real["checks"]))
    monkeypatch.setattr(screen, "raw_sizes", noted("raw_sizes", real["raw"]))
    monkeypatch.setattr(open_screen, "load_split", noted("questions", real["load"]))
    monkeypatch.setattr(preflight, "wait_clear", noted("wait_clear", lambda *_a, **_k: []))
    monkeypatch.setattr(runner, "check_fixture", noted("check_fixture", lambda _fx: None))
    monkeypatch.setattr(runner, "require_caps", noted("require_caps", lambda: None))
    monkeypatch.setattr(runner, "require_agent_tools", noted("tools", lambda _a: None))
    monkeypatch.setattr(runner, "run_trial", noted("trial", fake))
    go(tmp_path, profile="clean+v", experiment="exp-1")
    assert events[:14] == [
        "profile", "questions", "checks", "raw_sizes", "lock", "checks", "wait_clear",
        "check_fixture", "require_caps", "tools", "authorize", "bound", "sizes", "wait_clear",
    ]  # fmt: skip
    assert events[14] == "trial"
    assert asked == [("exp-1", {"baseline": "clean", "candidate": "clean+v"}, None)]
    assert {s.experiment_id for s in fake.specs} == {"exp-1"}


def files_in(tmp_path: Path) -> list[str]:
    return sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("r.jsonl"))


def test_a_variant_without_a_registration_is_refused_and_nothing_is_written(
    tmp_path: Path, fake: Fake, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(open_screen.Refusal, match="registered experiment"):
        go(tmp_path, profile="clean+v")
    assert fake.specs == [] and files_in(tmp_path) == []
    argv = ["run", "--split", "dev", "--models", SMALL, "--out", str(tmp_path / "r.jsonl")]
    argv += ["--sizes", str(tags_file(tmp_path)), "--profile", "clean+v"]
    assert open_screen.main(argv) == 2
    assert "refused:" in capsys.readouterr().err and files_in(tmp_path) == []


def test_a_plus_profile_naming_no_variant_is_refused(tmp_path: Path, fake: Fake) -> None:
    for exp in ("", "e-1"):
        with pytest.raises(open_screen.Refusal, match="names no variant"):
            go(tmp_path, profile="clean+", experiment=exp)
    assert fake.specs == [] and files_in(tmp_path) == []


def test_a_plain_clean_run_needs_no_registration(tmp_path: Path, fake: Fake) -> None:
    go(tmp_path)
    assert fake.specs and {s.experiment_id for s in fake.specs} == {""}


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"models": ["nope:1b"]}, "no disk size"),
        ({"models": [""]}, "nothing to run"),
        ({"repeats": 0}, "nothing to run"),
    ],
)
def test_a_bad_model_or_repeat_count_refuses_before_anything_is_written(
    tmp_path: Path, fake: Fake, kwargs: dict[str, Any], match: str
) -> None:
    with pytest.raises(open_screen.Refusal, match=match):
        go(tmp_path, **kwargs)
    assert fake.specs == [] and files_in(tmp_path) == []


def test_an_out_that_cannot_be_created_refuses_before_any_trial(tmp_path: Path, fake: Fake) -> None:
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        if os.access(ro, os.W_OK):
            pytest.skip("this user can write anywhere")
        with pytest.raises(open_screen.Refusal, match="cannot write"):
            go(tmp_path, ro / "sub" / "r.jsonl")
    finally:
        ro.chmod(0o700)
    assert fake.specs == [] and list(ro.iterdir()) == []


def test_a_missing_profile_is_a_refusal(
    tmp_path: Path, fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(_v: str, _p: str) -> object:
        raise FileNotFoundError("no manifest")

    monkeypatch.setattr(runner, "load_profile", missing)
    with pytest.raises(open_screen.Refusal, match="no such profile"):
        go(tmp_path)
    assert files_in(tmp_path) == []


def test_a_blocked_machine_refuses_unless_forced(
    tmp_path: Path, fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preflight, "wait_clear", lambda *_a, **_k: [SimpleNamespace(code="busy")])
    with pytest.raises(RuntimeError, match="busy"):
        go(tmp_path)
    assert fake.specs == [] and files_in(tmp_path) == []
    go(tmp_path, force=True)
    assert fake.specs


@pytest.mark.parametrize(
    ("split", "match"), [("final", "cannot read"), ("nope", "split must be"), ("", "split must be")]
)
def test_a_split_without_a_file_or_not_a_split_is_refused(
    tmp_path: Path, fake: Fake, split: str, match: str
) -> None:
    with pytest.raises(open_screen.Refusal, match=match):
        open_screen.run(split, [SMALL], 1, tmp_path / "r.jsonl", tags=tags_file(tmp_path))
    assert files_in(tmp_path) == []


@pytest.mark.parametrize(
    "content",
    [
        {**doc(), "split": "screen"},  # the file says another split than its name
        {**doc(), "questions": []},
        doc(ids=("T-1", "T-1")),
        {"split": "dev"},  # malformed
    ],
)
def test_an_unusable_question_file_is_refused(
    tmp_path: Path, fake: Fake, qdir: Path, content: dict[str, Any]
) -> None:
    (qdir / "dev.json").write_text(json.dumps(content))
    with pytest.raises(open_screen.Refusal):
        go(tmp_path)
    assert fake.specs == [] and files_in(tmp_path) == []


def test_the_split_and_seed_bind_to_the_out_file(tmp_path: Path, fake: Fake, qdir: Path) -> None:
    out = tmp_path / "r.jsonl"
    go(tmp_path, out)
    bound = json.loads(screen.questions_path(out).read_text())
    assert bound == doc() and open_screen.load_bound(out).split == "dev"
    n = len(fake.specs)
    (qdir / "dev.json").write_text(json.dumps(doc(seed=SEED + 1)))  # another seed
    with pytest.raises(open_screen.Refusal, match="split, seed or question set"):
        go(tmp_path, out)
    (qdir / "dev.json").write_text(json.dumps(doc(ids=("T-1", "N-1"))))  # another question set
    with pytest.raises(open_screen.Refusal, match="split, seed or question set"):
        go(tmp_path, out)
    with pytest.raises(open_screen.Refusal, match="split, seed or question set"):
        open_screen.run("screen", [SMALL], 1, out, tags=tags_file(tmp_path))  # another split
    assert len(fake.specs) == n


def test_a_resume_over_another_profile_or_experiment_is_refused(
    tmp_path: Path, fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(registry, "authorize", lambda e, *_a, **_k: e)
    out = tmp_path / "r.jsonl"
    go(tmp_path, out, profile="clean+v", experiment="e-1")
    n = len(fake.specs)
    with pytest.raises(open_screen.Refusal, match="profile"):
        go(tmp_path, out)  # plain clean over a variant's rows
    with pytest.raises(open_screen.Refusal, match="experiment"):
        go(tmp_path, out, profile="clean+v", experiment="e-2")
    assert len(fake.specs) == n


def test_the_checks_are_repeated_under_the_lock_against_a_racing_session(
    tmp_path: Path, fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "r.jsonl"

    @contextlib.contextmanager
    def racing() -> Any:
        out.write_text(json.dumps({"fixture_profile": "clean+rule", "experiment_id": ""}) + "\n")
        yield

    monkeypatch.setattr(preflight, "session_lock", racing)
    with pytest.raises(open_screen.Refusal, match="profile"):
        go(tmp_path, out)
    assert fake.specs == [] and not screen.questions_path(out).exists()


def test_main_runs_with_the_default_out_under_the_m9_results_folder(
    tmp_path: Path, fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(open_screen.layout, "RESULTS", tmp_path / "results")
    argv = ["run", "--split", "dev", "--models", f"{SMALL},", "--repeats", "2"]
    assert open_screen.main([*argv, "--sizes", str(tags_file(tmp_path))]) == 0
    out = tmp_path / "results" / "research" / "m9" / "dev-clean.jsonl"
    assert out.exists() and len(fake.specs) == 2 + 6
    assert open_screen.default_out("final", "clean+v").name == "final-clean+v.jsonl"
    assert (
        open_screen.default_judgments()
        == tmp_path / "results" / "research" / "m9" / "judgments.jsonl"
    )


# ---- the load of a trial -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "ctx"),
    [
        (SMALL, 65536),
        (BIG, 65536),
        ("qwen3.6:27b", 32768),
        ("x:7b-64k:latest", 65536),
        ("x:7b-8k", 8192),
        ("x:7b-q4_K_M", 32768),
        ("64k:7b", 32768),
    ],
)
def test_the_context_comes_from_a_k_suffix_on_the_tag(model: str, ctx: int) -> None:
    assert open_screen.context_tokens(model) == ctx


def test_trial_load_takes_the_largest_prompt_and_body_and_tolerates_gaps(tmp_path: Path) -> None:
    adir = write_artifact(tmp_path, "a", "x", inputs=[100, 900, 500], bodies=[10, 5000, 300])
    assert open_screen.trial_load(adir) == open_screen.Load(900, 5000)
    (adir / "ollama_trace.jsonl").write_text('{"body_len": 7}\nnot json\n{"body_len": true}\n')
    assert open_screen.trial_load(adir) == open_screen.Load(900, 7)
    assert open_screen.trial_load(write_artifact(tmp_path, "b", "x")) == open_screen.Load(0, 0)
    odd = tmp_path / "c"
    odd.mkdir()
    (odd / "transcript.jsonl").write_text(
        '{"type": "step_finish", "part": {"tokens": {"input": true}}}\n'
        '{"type": "step_finish", "part": null}\n'
        '{"type": "text", "part": {"tokens": {"input": 99999}}}\n'
        "a truncated line {\n"
        '{"type": "step_finish", "part": {"tokens": {"input": 12}}}\n'
    )
    assert open_screen.trial_load(odd) == open_screen.Load(12, 0)
    (odd / "transcript.jsonl").write_text(
        '{"type": "step_finish", "part": {"tokens": {"input": true}}}\n'
    )
    assert open_screen.trial_load(odd) == open_screen.Load(0, 0)  # a boolean is not a count
    with pytest.raises(OSError):
        open_screen.trial_load(tmp_path / "gone")


def test_a_context_is_flagged_at_three_quarters_of_it_or_when_the_body_cannot_fit() -> None:
    load = open_screen.Load
    assert not open_screen.context_filled(SMALL, load(49151, 262144))
    assert open_screen.context_filled(SMALL, load(49152, 0))
    assert open_screen.context_filled(SMALL, load(0, 262145))
    assert open_screen.context_filled("x:1b", load(24576, 0))  # no suffix: 32768
    assert not open_screen.context_filled("x:1b", load(24575, 131072))
    assert open_screen.context_filled(
        "x:1b", load(26195, 145000)
    )  # the lookup run that lost its question


@pytest.mark.parametrize(
    "text",
    [
        "I don't see a question in your message.",
        "I do not see any question here. What would you like to know?",
        "I didn’t receive a question.",
        "We can't find your question.",
        "There is no question in this conversation.",
        "No question was provided.",
        "The question is missing.",
        "the QUESTION got cut off",
        "  I don't see a question  ",
    ],
)
def test_a_short_reply_that_says_there_is_no_question_is_a_lost_question(text: str) -> None:
    assert open_screen.lost_question(text)


@pytest.mark.parametrize(
    "text",
    [
        "The alpha helper calls beta, see pkg/mod.py:1.",
        "I don't know the answer to that question.",
        "There is no gamma cache in the repo.",
        "I don't see a question of cost here, only latency.".replace(
            "question of cost", "gamma cache"
        ),
        "",
        "I don't see a question. " + "x" * 500,  # a long answer is never a lost question
        "I don't see a question. " + "x" * 600,
    ],
)
def test_other_text_is_not_a_lost_question(text: str) -> None:
    assert not open_screen.lost_question(text)


def test_a_lost_question_reply_of_a_few_hundred_characters_still_counts() -> None:
    assert open_screen.lost_question("I don't see a question. " + "Please ask it again. " * 14)


def test_a_reply_of_exactly_the_length_limit_still_counts() -> None:
    text = "I don't see a question." + " " * 0 + "x" * (open_screen.LOST_MAX_CHARS - 23)
    assert len(text) == open_screen.LOST_MAX_CHARS and open_screen.lost_question(text)
    assert not open_screen.lost_question(text + "x")


# ---- export-answers ----------------------------------------------------------------------------

Spec = tuple[str, str, int, str]  # model, question, repeat, text


def results_file(
    tmp_path: Path,
    trials: Sequence[Spec],
    name: str = "res.jsonl",
    outcomes: dict[int, str] | None = None,
    load: dict[int, tuple[Sequence[int], Sequence[int]]] | None = None,
    secs: float = 10.0,
) -> Path:
    """Rows with artifacts for (model, question, repeat, text), bound to the dev questions."""
    out = tmp_path / name
    lines = []
    for i, (model, qid, repeat, text) in enumerate(trials):
        inputs, bodies = (load or {}).get(i, ((), ()))
        adir = write_artifact(tmp_path / "art" / name, f"t{i}", text, inputs, bodies)
        lines.append(
            {
                "trial_id": f"t{i}",
                "label": qid,
                "arm": model,
                "repeat": repeat,
                "outcome": (outcomes or {}).get(i, "completed"),
                "secs": secs + i,
                "artifact": str(adir),
            }  # fmt: skip
        )
    lines.append({"trial_id": "w", "label": "warmup-1", "arm": trials[0][0], "repeat": 0,
                  "outcome": "completed", "secs": 1.0, "artifact": str(adir)})  # fmt: skip
    out.write_text("".join(json.dumps(r) + "\n" for r in lines))
    open_screen.write_bound(out, doc())
    return out


GOOD = {
    "T-1": "alpha calls beta, see pkg/mod.py:1-2.",
    "N-1": "There is no gamma cache anywhere.",
    "E-1": "delta is read from the config; see beta in pkg/mod.py:5.",
}


def test_export_writes_blinded_answers_and_keeps_the_models_in_a_separate_map(
    tmp_path: Path,
) -> None:
    out = results_file(
        tmp_path,
        [(SMALL, "T-1", 1, GOOD["T-1"]), (BIG, "T-1", 1, GOOD["T-1"]), (BIG, "N-1", 1, GOOD["N-1"]),
         (SMALL, "T-1", 2, "a different answer")],
    )  # fmt: skip
    answers, mapping = tmp_path / "a" / "answers.jsonl", tmp_path / "m" / "map.json"
    assert open_screen.export_answers([out], answers, mapping) == 3
    rows = [json.loads(line) for line in answers.read_text().splitlines()]
    assert [r["answer_id"] for r in rows] == sorted(r["answer_id"] for r in rows)
    assert all(set(r) == {"question_id", "answer_id", "text"} for r in rows)
    assert len(open_judge.load_answers(answers)) == 3  # the judge reads exactly this file
    text = answers.read_text()
    assert SMALL not in text and BIG not in text and "qwen" not in text and "laguna" not in text
    assert "res.jsonl" not in text
    by_answer = {r["text"]: r for r in rows}
    shared = by_answer[GOOD["T-1"]]
    assert shared["question_id"] == "T-1"
    assert shared["answer_id"] == open_screen.answer_id("T-1", open_screen.answer_hash(GOOD["T-1"]))
    who = json.loads(mapping.read_text())
    assert sorted(w["model"] for w in who[shared["answer_id"]]) == sorted([BIG, SMALL])
    assert who[by_answer["a different answer"]["answer_id"]] == [
        {"model": SMALL, "question": "T-1", "repeat": 2, "results": "res.jsonl"}
    ]
    assert open_screen.export_answers([out], answers, mapping) == 3  # the same again
    assert answers.read_text() == text


def test_the_blind_id_depends_on_the_question_and_the_text_only() -> None:
    digest = open_screen.answer_hash("x")
    assert len(digest) == 64 and digest == open_screen.answer_hash("x") != open_screen.answer_hash(
        "y"
    )
    one = open_screen.answer_id("T-1", digest)
    assert one.startswith("a-") and len(one) == 14
    assert one == open_screen.answer_id("T-1", digest) != open_screen.answer_id("N-1", digest)
    assert one != open_screen.answer_id("T-1", open_screen.answer_hash("y"))


def test_export_leaves_out_what_needs_no_judge_or_was_never_finished(tmp_path: Path) -> None:
    trials = [
        (SMALL, "T-1", 1, ""),  # no text
        (SMALL, "T-1", 2, '{"name": "read", "arguments": {}}'),  # a tool call
        (SMALL, "N-1", 1, "I don't see a question."),  # a lost question
        (SMALL, "E-1", 1, GOOD["E-1"]),
        (SMALL, "E-1", 2, "timed out text"),
        (SMALL, "N-1", 2, "silent"),
    ]
    out = results_file(tmp_path, trials, outcomes={4: "timeout", 5: "silent_stall"})
    answers = tmp_path / "answers.jsonl"
    assert open_screen.export_answers([out], answers, tmp_path / "map.json") == 2
    kept = {json.loads(line)["text"] for line in answers.read_text().splitlines()}
    assert kept == {GOOD["E-1"], "silent"}


def test_export_over_two_results_files_and_a_map_that_may_not_be_the_answers_file(
    tmp_path: Path,
) -> None:
    one = results_file(tmp_path, [(SMALL, "T-1", 1, GOOD["T-1"])], "one.jsonl")
    two = results_file(tmp_path, [(MID, "E-1", 1, GOOD["E-1"])], "two.jsonl")
    answers, mapping = tmp_path / "answers.jsonl", tmp_path / "map.json"
    assert open_screen.export_answers([one, two], answers, mapping) == 2
    assert sorted(w["results"] for ws in json.loads(mapping.read_text()).values() for w in ws) == [
        "one.jsonl",
        "two.jsonl",
    ]
    with pytest.raises(open_screen.Refusal, match="different files"):
        open_screen.export_answers([one], answers, answers)
    with pytest.raises(open_screen.Refusal, match="no usable questions"):
        open_screen.export_answers([tmp_path / "unbound.jsonl"], answers, mapping)


def test_main_export_prints_the_count_and_writes_both_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = results_file(tmp_path, [(SMALL, "T-1", 1, GOOD["T-1"])])
    answers = tmp_path / "answers.jsonl"
    assert open_screen.main(["export-answers", str(out), "--answers", str(answers)]) == 0
    assert "1 answers" in capsys.readouterr().out and answers.exists()
    assert (tmp_path / "answers.jsonl.map.json").exists()
    assert open_screen.main(["export-answers", str(out), "--answers", str(answers),
                             "--mapping", str(answers)]) == 2  # fmt: skip


# ---- the report --------------------------------------------------------------------------------


def verdict(**over: Any) -> dict[str, Any]:
    """Every fact stated, no claim made, no candidate asserted."""
    base: dict[str, Any] = {
        "stated": {f: True for f in FACT},
        "made": {c: False for c in CLAIM},
        "asserted": {},
    }
    return {**base, **over}


J1, J2 = "judge-one", "judge-two"


def judged(
    qid: str,
    text: str,
    judge: str = J1,
    template: str | None = None,
    status: str = "ok",
    reason: str = "",
    **fields: Any,
) -> dict[str, Any]:
    """A judgment row as the judge writes it (`open_judge`'s own writer), with verdict overrides."""
    facts = {"T-1": ["t1", "t2"], "N-1": ["n1"], "E-1": ["e1", "e2"]}[qid]
    claim = {"T-1": "c1", "N-1": "m1", "E-1": "x1"}[qid]
    v = verdict(stated={f: True for f in facts}, made={claim: False}) | fields
    verdicts = None if status == "failed" else open_grade.Verdicts(
        v["stated"], v["made"], v["asserted"]
    )  # fmt: skip
    tpl = template or open_judge.template_hash()
    row = open_judge.Judgment(qid, open_screen.answer_hash(text), judge, tpl, "in", status,
                              verdicts, reason, 0.01)  # fmt: skip
    return open_judge._judgment_row(row)


def write_judgments(path: Path, rows: Sequence[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def all_judged(texts: dict[str, str] = GOOD) -> list[dict[str, Any]]:
    return [judged(q, t) for q, t in texts.items()]


def rep(out: Path, tree: Path, judgments: Path, judge_model: str = J1) -> str:
    return open_screen.report(out, judgments, tree, judge_model)


def test_report_scores_a_judged_model_and_never_prints_question_or_fact_text(
    tmp_path: Path, tree: Path
) -> None:
    trials = [(SMALL, q, r, t) for r in range(1, 13) for q, t in GOOD.items()]  # 36 trials
    out = results_file(tmp_path, trials)
    text = rep(out, tree, write_judgments(tmp_path / "j.jsonl", all_judged()))
    assert "| qwen3.6:27b-64k | all | 36 | 0 | 0 | 1.00 [1.00, 1.00] | 1.00 [1.00, 1.00] |" in text
    assert "| 0/36 (<= 0.10) | 28 |" in text and "PASS" in text and "FAIL" not in text
    for kind in ("Trace", "Negative", "Explain"):
        assert f"| qwen3.6:27b-64k | {kind} | 12 |" in text
    assert (
        "Split dev, seed 41: 3 questions (Trace 1, Explain 1, Conflict 0, Negative 1, Locate-all 0)"
        in text
    )
    for secret in [*TEXT.values(), *FACT.values(), *CLAIM.values()]:
        assert secret not in text
    graded = [json.loads(x) for x in screen.graded_path(out).read_text().splitlines()]
    assert len(graded) == 36 and {g["status"] for g in graded} == {"graded"}
    assert all(secret not in screen.graded_path(out).read_text() for secret in TEXT.values())


def test_a_forbidden_claim_is_silent_wrong_and_fails_the_bar_with_a_missing_answer_apart(
    tmp_path: Path, tree: Path
) -> None:
    trials = [(BIG, "T-1", 1, GOOD["T-1"]), (BIG, "N-1", 1, GOOD["N-1"]), (BIG, "E-1", 1, "")]
    out = results_file(tmp_path, trials)
    rows = all_judged()
    rows[0] = judged("T-1", GOOD["T-1"], made={"c1": True}, stated={"t1": True, "t2": False})
    text = rep(out, tree, write_judgments(tmp_path / "j.jsonl", rows))
    assert "| laguna-xs-2.1:q4_K_M-64k | all | 3 | 1 | 0 |" in text
    assert "1/2 (<= " in text and "FAIL" in text and "PASS" not in text
    graded = {
        g["question"]: g for g in map(json.loads, screen.graded_path(out).read_text().splitlines())
    }
    assert graded["T-1"]["silent_wrong"] is True and graded["T-1"]["forbidden_made"] == ["c1"]
    assert graded["T-1"]["recall"] == 0.5
    assert graded["E-1"]["status"] == "no_answer" and graded["E-1"]["silent_wrong"] is False
    assert graded["E-1"]["recall"] == 0.0


def test_a_trial_without_a_usable_judgment_is_unjudged_and_never_scored(
    tmp_path: Path, tree: Path
) -> None:
    trials = [(SMALL, "T-1", 1, GOOD["T-1"]), (SMALL, "N-1", 1, GOOD["N-1"]),
              (SMALL, "E-1", 1, GOOD["E-1"]), (SMALL, "E-1", 2, GOOD["E-1"] + " more")]  # fmt: skip
    out = results_file(tmp_path, trials)
    rows = [
        judged("T-1", GOOD["T-1"], status="failed", reason="rate limited"),  # failed
        judged("N-1", GOOD["N-1"], stated={}),  # does not cover the rubric
        # E-1 repeat 1: judged; repeat 2: no judgment at all
        judged("E-1", GOOD["E-1"]),
    ]
    text = rep(out, tree, write_judgments(tmp_path / "j.jsonl", rows))
    assert "| T-1 | 1 | failed judgment |" in text
    assert "| N-1 | 1 | invalid verdicts:" in text
    assert "| E-1 | 2 | no judgment |" in text
    assert "| qwen3.6:27b-64k | all | 1 | 0 | 3 |" in text
    assert "FAIL (provisional: 3 unjudged)" in text
    graded = [json.loads(x) for x in screen.graded_path(out).read_text().splitlines()]
    assert sorted(g["status"] for g in graded) == ["graded", "unjudged", "unjudged", "unjudged"]
    assert not [g for g in graded if g["status"] == "unjudged" and "recall" in g]
    assert rep(out, tree, tmp_path / "none.jsonl").count("no judgment") == 4  # no file


def test_a_retried_judgment_stands_and_a_row_off_the_schema_is_no_judgment(
    tmp_path: Path, tree: Path
) -> None:
    out = results_file(tmp_path, [(SMALL, "T-1", 1, GOOD["T-1"]), (SMALL, "N-1", 1, GOOD["N-1"])])
    rows = [
        judged("T-1", GOOD["T-1"], status="failed", reason="boom"),
        judged("T-1", GOOD["T-1"], stated={"t1": True, "t2": False}),  # the retry, later
        judged("T-1", GOOD["T-1"], status="failed", reason="a later failure"),
        {"question": "N-1", "answer_hash": open_screen.answer_hash(GOOD["N-1"]), "stated": 3},
    ]
    text = rep(out, tree, write_judgments(tmp_path / "j.jsonl", rows))
    assert "| N-1 | 1 | no judgment |" in text
    graded = {
        g["question"]: g for g in map(json.loads, screen.graded_path(out).read_text().splitlines())
    }
    assert graded["T-1"]["recall"] == 0.5  # the latest ok row stands over a later failure


def test_pick_verdicts_says_why_there_are_none() -> None:
    def row(**kw: Any) -> open_judge.Judgment:
        got = open_judge.judgment_from_row(judged("T-1", GOOD["T-1"], **kw))
        assert got
        return got

    old = "an-older-template"
    assert open_screen.pick_verdicts([]) == (None, "no judgment")
    assert open_screen.pick_verdicts([row(status="failed")]) == (None, "failed judgment")
    assert open_screen.pick_verdicts([row(template=old)])[1].startswith("stale judgment")
    ok = row()
    assert open_screen.pick_verdicts([row(template=old), ok]) == (ok.verdicts, "")
    # a stale ok row never stands in for a current failure
    assert open_screen.pick_verdicts([row(template=old), row(status="failed")])[1] == (
        "failed judgment"
    )


def test_a_failed_judgment_is_listed_as_failed_and_never_as_malformed(
    tmp_path: Path, tree: Path
) -> None:
    out = results_file(tmp_path, [(SMALL, "T-1", 1, GOOD["T-1"])])
    rows = [judged("T-1", GOOD["T-1"], status="failed", reason="invalid output")]
    text = rep(out, tree, write_judgments(tmp_path / "j.jsonl", rows))
    assert "| T-1 | 1 | failed judgment |" in text and "malformed" not in text


def test_a_row_from_an_older_template_is_listed_as_stale_and_not_scored(
    tmp_path: Path, tree: Path
) -> None:
    out = results_file(tmp_path, [(SMALL, "T-1", 1, GOOD["T-1"]), (SMALL, "N-1", 1, GOOD["N-1"])])
    rows = [judged("T-1", GOOD["T-1"], template="older"), judged("N-1", GOOD["N-1"])]
    text = rep(out, tree, write_judgments(tmp_path / "j.jsonl", rows))
    assert "| T-1 | 1 | stale judgment: written under an older template |" in text
    assert "| qwen3.6:27b-64k | all | 1 | 0 | 1 |" in text
    graded = {
        g["question"]: g for g in map(json.loads, screen.graded_path(out).read_text().splitlines())
    }
    assert graded["T-1"]["status"] == "unjudged" and "recall" not in graded["T-1"]
    assert graded["N-1"]["status"] == "graded"


def test_the_report_needs_a_judge_and_scores_with_that_judge_only(
    tmp_path: Path, tree: Path
) -> None:
    out = results_file(tmp_path, [(SMALL, "T-1", 1, GOOD["T-1"])])
    rows = [
        judged("T-1", GOOD["T-1"], judge=J1, stated={"t1": True, "t2": False}),
        judged("T-1", GOOD["T-1"], judge=J2),
    ]
    path = write_judgments(tmp_path / "j.jsonl", rows)
    with pytest.raises(open_screen.Refusal, match="--judge-model"):
        rep(out, tree, path, "")
    for judge, recall in ((J1, 0.5), (J2, 1.0)):
        assert f"{recall:.2f} [{recall:.2f}, {recall:.2f}]" in rep(out, tree, path, judge)
    assert "| T-1 | 1 | no judgment |" in rep(out, tree, path, "nobody")


def test_the_latest_of_one_judges_rows_stands_and_a_bad_line_is_skipped(
    tmp_path: Path, tree: Path
) -> None:
    out = results_file(tmp_path, [(SMALL, "T-1", 1, GOOD["T-1"])])
    first = judged("T-1", GOOD["T-1"], stated={"t1": False, "t2": False})
    later = judged("T-1", GOOD["T-1"], stated={"t1": True, "t2": False})
    other = judged("T-1", GOOD["T-1"], judge=J2)
    one = write_judgments(tmp_path / "one.jsonl", [first, other, later])
    assert "0.50 [0.50, 0.50]" in rep(out, tree, one)
    broken = tmp_path / "broken.jsonl"
    broken.write_text("not json {\n" + json.dumps(later) + "\n[1, 2]\n")
    assert "0.50 [0.50, 0.50]" in rep(out, tree, broken)  # a bad line is skipped, not fatal


def test_a_fabricated_citation_the_judge_marks_asserted_is_silent_wrong(
    tmp_path: Path, tree: Path
) -> None:
    ghost = "alpha calls beta, see pkg/ghost.py:3."
    out = results_file(tmp_path, [(SMALL, "T-1", 1, ghost), (SMALL, "T-1", 2, ghost + " ")])
    on = judged("T-1", ghost, asserted={"pkg/ghost.py:3": True})
    off = judged("T-1", ghost + " ", asserted={"pkg/ghost.py:3": False})
    text = rep(out, tree, write_judgments(tmp_path / "j.jsonl", [on, off]))
    graded = {
        g["repeat"]: g for g in map(json.loads, screen.graded_path(out).read_text().splitlines())
    }
    assert graded[1]["silent_wrong"] is True and graded[1]["fabrications"] == 1
    assert graded[1]["citation_valid"] == 0.0
    assert graded[2]["silent_wrong"] is False and graded[2]["citations_total"] == 0
    assert "1/2 (<= " in text


def test_a_lost_question_is_no_answer_flagged_and_not_wrong_and_a_full_context_is_flagged(
    tmp_path: Path, tree: Path
) -> None:
    trials = [(SMALL, "T-1", 1, "I don't see a question in your message."),
              (SMALL, "N-1", 1, GOOD["N-1"]), (SMALL, "E-1", 1, GOOD["E-1"])]  # fmt: skip
    out = results_file(
        tmp_path, trials, load={1: ([30000, 52000], [900, 270000]), 2: ([100], [5000])}
    )
    text = rep(out, tree, write_judgments(tmp_path / "j.jsonl", all_judged()))
    assert "| qwen3.6:27b-64k | T-1 | 1 | 0 | 65536 | 0 | lost question |" in text
    assert "| qwen3.6:27b-64k | N-1 | 1 | 52000 | 65536 | 270000 | context filled |" in text
    assert "E-1 | 1 | 100" not in text
    graded = {
        g["question"]: g for g in map(json.loads, screen.graded_path(out).read_text().splitlines())
    }
    assert graded["T-1"]["status"] == "no_answer" and graded["T-1"]["lost_question"] is True
    assert graded["T-1"]["silent_wrong"] is False and graded["T-1"]["recall"] == 0.0
    assert graded["N-1"]["context_filled"] is True and graded["N-1"]["lost_question"] is False
    assert (graded["N-1"]["max_prompt_tokens"], graded["N-1"]["max_body_bytes"]) == (52000, 270000)
    assert graded["E-1"]["context_filled"] is False
    both = results_file(tmp_path, [(SMALL, "T-1", 1, "No question was given.")], "both.jsonl",
                        load={0: ([60000], [])})  # fmt: skip
    assert "context filled and lost question" in rep(both, tree, tmp_path / "none.jsonl")


def test_excluded_trials_are_listed_with_their_reason_and_a_retry_replaces_its_cell(
    tmp_path: Path, tree: Path
) -> None:
    trials = [(SMALL, "T-1", 1, GOOD["T-1"]), (SMALL, "N-1", 1, GOOD["N-1"]), (SMALL, "E-1", 1, ""),
              (SMALL, "T-1", 1, GOOD["T-1"])]  # fmt: skip
    out = results_file(
        tmp_path, trials, outcomes={0: "harness_error", 1: "timeout", 2: "silent_stall"}
    )
    text = rep(out, tree, write_judgments(tmp_path / "j.jsonl", all_judged()))
    assert "| qwen3.6:27b-64k | N-1 | 1 | timeout |" in text
    assert "T-1 | 1 | harness_error" not in text  # the retry stands
    assert "| qwen3.6:27b-64k | all | 2 | 1 | 0 |" in text  # T-1 graded, E-1 silent: missing
    gone = results_file(tmp_path, [(MID, "T-1", 1, GOOD["T-1"])], "gone.jsonl")
    for p in (tmp_path / "art" / "gone.jsonl").iterdir():
        for f in p.iterdir():
            f.unlink()
        p.rmdir()
    assert "| gemma4:26b-64k | T-1 | 1 | no_artifact |" in rep(gone, tree, tmp_path / "none.jsonl")


def test_a_model_with_every_trial_excluded_still_has_a_row_and_fails(
    tmp_path: Path, tree: Path
) -> None:
    out = results_file(tmp_path, [(MID, "T-1", 1, "x")], outcomes={0: "timeout"})
    text = rep(out, tree, tmp_path / "none.jsonl")
    assert "| gemma4:26b-64k | all | 0 | 0 | 0 | - | - | 0/0 (<= 1.00) | - |" in text
    assert "| gemma4:26b-64k | nan | nan | 1.00 | FAIL" in text


def test_summarize_follows_the_stats_helpers_and_median_covers_unjudged_trials() -> None:
    def row(
        q: str, recall: float, cite: float, silent: bool, status: str = "graded", secs: float = 1.0
    ):
        return {"question": q, "status": status, "recall": recall, "citation_valid": cite,
                "silent_wrong": silent, "seconds": secs}  # fmt: skip

    rows = [row("a", 1.0, 1.0, False, secs=10), row("a", 0.5, 0.5, True, secs=20),
            row("b", 0.25, 0.0, False, secs=30), row("c", 0.0, 0.0, False, "no_answer", secs=40),
            {"question": "d", "status": "unjudged", "seconds": 50.0}]  # fmt: skip
    s = open_screen.summarize(rows)
    assert (s.scored, s.missing, s.unjudged, s.answered, s.silent) == (4, 1, 1, 3, 1)
    assert s.upper == stats.wilson(1, 3)[1]
    assert s.recall == stats.cluster_ci({"a": [1.0, 0.5], "b": [0.25], "c": [0.0]})
    assert s.citation == stats.cluster_ci({"a": [1.0, 0.5], "b": [0.0], "c": [0.0]})
    assert s.median == 30.0
    assert open_screen.summarize([]).scored == 0 and not open_screen.summarize([]).passes


@pytest.mark.parametrize(
    ("recall", "cite", "silent", "passes"),
    [
        (0.8, 0.95, 0, True),  # both bars met exactly
        (0.79, 0.95, 0, False),
        (0.8, 0.94, 0, False),
        (1.0, 1.0, 1, False),  # one silent_wrong in 40 has a Wilson upper bound above 0.10
    ],
)
def test_the_o2_bar_is_recall_citations_and_the_silent_wrong_upper_bound(
    recall: float, cite: float, silent: int, passes: bool
) -> None:
    rows = [
        {"question": q, "status": "graded", "recall": recall, "citation_valid": cite,
         "silent_wrong": i < silent, "seconds": 1.0}
        for i in range(40) for q in [f"q{i % 2}"]
    ]  # fmt: skip
    assert open_screen.summarize(rows).passes is passes


def test_an_upper_bound_exactly_at_the_bar_passes() -> None:
    s = open_screen.summarize([])
    at_bar = open_screen.Summary(
        40, 0, 0, 40, 0, open_screen.SILENT_UPPER_BAR, (1.0, 1.0, 1.0), (1.0, 1.0, 1.0), 1.0
    )
    assert at_bar.passes and not dataclasses.replace(at_bar, upper=0.1000001).passes
    assert s.upper == 1.0


def test_too_few_answered_trials_cannot_pass_the_silent_wrong_bound() -> None:
    def good(i: int) -> dict[str, Any]:
        return {"question": f"q{i % 2}", "status": "graded", "recall": 1.0, "citation_valid": 1.0,
                "silent_wrong": False, "seconds": 1.0}  # fmt: skip

    assert open_screen.summarize([good(i) for i in range(35)]).passes  # Wilson upper 0.0916
    assert not open_screen.summarize([good(i) for i in range(34)]).passes  # 0.1019


def test_main_report_prints_and_refuses_an_unbound_results_file(
    tmp_path: Path, tree: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    out = results_file(tmp_path, [(SMALL, "T-1", 1, GOOD["T-1"])])
    path = write_judgments(tmp_path / "j.jsonl", all_judged())
    argv = ["report", str(out), "--judgments", str(path), "--tree", str(tree)]
    assert open_screen.main([*argv, "--judge-model", J1]) == 0
    assert "# Open-ended research screen (M9)" in capsys.readouterr().out
    assert open_screen.main([*argv, "--judge-model", "j9"]) == 0
    assert "no judgment" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        open_screen.main(argv)  # the judge is required
    with pytest.raises(SystemExit):
        open_screen.main([*argv, "--judge-model", J1, "--prompt-hash", "p1"])  # no such option
    monkeypatch.setattr(open_screen, "default_tree", lambda: tree)
    monkeypatch.setattr(open_screen.layout, "RESULTS", tmp_path / "results")
    assert open_screen.main(["report", str(out), "--judge-model", J1]) == 0  # no judgments file
    assert "no judgment" in capsys.readouterr().out
    assert open_screen.main(["report", str(tmp_path / "unbound.jsonl"), "--judge-model", J1]) == 2
    assert "refused:" in capsys.readouterr().err


# ---- an unregistered run never extends a registered experiment's file --------------------------


def test_an_unregistered_run_cannot_extend_a_file_holding_a_registered_experiments_rows(
    tmp_path: Path, fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(registry, "authorize", lambda e, *_a, **_k: e)
    out = tmp_path / "r.jsonl"
    go(tmp_path, out, experiment="e-1")  # a registered clean run
    before, n = out.read_text(), len(fake.specs)
    with pytest.raises(open_screen.Refusal, match=r"holds rows of experiment \['e-1'\]"):
        go(tmp_path, out, repeats=2)  # empty --experiment over the same file
    assert out.read_text() == before and len(fake.specs) == n
    open_screen.check_unregistered(tmp_path / "absent.jsonl", "")  # nothing to extend
    open_screen.check_unregistered(out, "e-1")  # registered over its own rows: not this check


def test_the_registered_experiment_check_also_runs_under_the_lock(
    tmp_path: Path, fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "r.jsonl"

    @contextlib.contextmanager
    def racing() -> Any:
        out.write_text(json.dumps({"fixture_profile": "clean", "experiment_id": "e-9"}) + "\n")
        yield

    monkeypatch.setattr(preflight, "session_lock", racing)
    with pytest.raises(open_screen.Refusal, match="e-9"):
        go(tmp_path, out)
    assert fake.specs == [] and not screen.questions_path(out).exists()


# ---- export, judge, report and audit together, on rows written the way the runner writes them --

# The field list of one real trial row (`runner.run_trial`'s record), with its value types; the
# values below are synthetic.
RUNNER_FIELDS = {
    "agent": str, "answer_kind": str, "arm": str, "artifact": str, "artifact_sha256": str,
    "config_sha256": str, "data_fingerprint": str, "detail": str, "deviations": list,
    "environment": str, "events": int, "experiment": dict, "experiment_id": str, "final_text": str,
    "fixture_base_commit": str, "fixture_profile": str, "fixture_tree_hash": str,
    "fixture_version": str, "git_hash": str, "git_primary": bool, "gpu": str,
    "harness_commit": str, "harness_dirty": bool, "harness_tree": str, "hooks": list,
    "issue_hash": str, "issue_ids": list, "label": str, "limits": bool, "model": str,
    "model_digest": str, "model_parameters": str, "net": str, "ollama_server": dict,
    "ollama_version": str, "opencode_version": str, "outcome": str, "phases": dict,
    "preflight_after": list, "preflight_forced": list, "prompt": str, "rc": int, "repeat": int,
    "rules_hash": str, "schema": int, "secs": float, "seed": type(None), "seeded": bool,
    "source_commit": str, "steps": int, "tokens_in": int, "tokens_out": int, "tool_calls": int,
    "tool_errors": int, "trial_id": str, "variant_hash": str, "venv_fingerprint": str,
    "written_files": int,
}  # fmt: skip


def runner_row(**over: Any) -> dict[str, Any]:
    """A trial row with every field the runner writes, synthetic values, and `over` on top."""
    row = {name: kind() if kind is not type(None) else None for name, kind in RUNNER_FIELDS.items()}
    assert set(over) <= set(RUNNER_FIELDS), set(over) - set(RUNNER_FIELDS)
    return {**row, "schema": 1, "fixture_profile": "clean", "experiment_id": "", **over}


PARTIAL = "partial: delta is read from the config; see beta in pkg/mod.py:5."
GHOST_ANSWER = "alpha calls beta, see pkg/ghost.py:3."
# (model, question, repeat, outcome, text)
TRIALS = [
    (SMALL, "T-1", 1, "completed", GOOD["T-1"]),
    (BIG, "T-1", 1, "completed", GOOD["T-1"]),  # the same answer text as the first
    (SMALL, "N-1", 1, "completed", GOOD["N-1"]),
    (SMALL, "E-1", 1, "completed", PARTIAL),
    (BIG, "E-1", 1, "completed", ""),  # a missing answer
    (SMALL, "T-1", 2, "completed", GHOST_ANSWER),
    (BIG, "N-1", 1, "timeout", "cut off"),  # excluded
]  # fmt: skip
JA, JB = "claude-haiku-5-5", "claude-sonnet-5-5"


def runner_results(tmp_path: Path, split: str, name: str) -> Path:
    out = tmp_path / name
    lines = []
    for i, (model, qid, repeat, outcome, text) in enumerate(TRIALS):
        adir = write_artifact(tmp_path / "art" / name, f"t{i}", text)
        lines.append(
            runner_row(
                arm=model,
                model=model,
                label=qid,
                repeat=repeat,
                outcome=outcome,
                trial_id=f"t{i}",
                artifact=str(adir),
                secs=10.0 + i,
                prompt=TEXT[qid],
                final_text=text,
            )  # fmt: skip
        )
    out.write_text("".join(json.dumps(r) + "\n" for r in lines))
    open_screen.write_bound(out, doc(split))
    return out


class Stub:
    """The judge's model: judge A states every fact the answer does not call `partial` and asserts
    every candidate; judge B states only the first fact of a question and asserts none."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(
        self, model: str, system: str, prompt: str, budget: float
    ) -> open_judge.CallResult:
        self.calls.append(model)
        ids = {
            k: ast.literal_eval(v) for k, v in re.findall(r"^(\w+) ids: (\[.*\])$", prompt, re.M)
        }
        stated, made, asserted = ids["stated"], ids["made"], ids["asserted"]
        if model == JA:
            partial = "partial:" in prompt
            facts = {f: not (partial and f.endswith("2")) for f in stated}
            cites = {c: True for c in asserted}
        else:
            facts = {f: f == stated[0] for f in stated}
            cites = {c: False for c in asserted}
        body = {"stated": facts, "made": {c: False for c in made}, "asserted": cites}
        return open_judge.CallResult(json.dumps(body), 0.01)


def patch_judge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub: Stub) -> Path:
    monkeypatch.setattr(open_judge, "claude_call", stub)
    monkeypatch.setattr(open_judge, "LEDGER", tmp_path / "m9" / "spend.jsonl")
    monkeypatch.setattr(open_judge, "JUDGMENTS", tmp_path / "m9" / "judgments.jsonl")
    return tmp_path / "m9" / "judgments.jsonl"


def graded_by_trial(out: Path) -> dict[tuple[str, str, int], dict[str, Any]]:
    rows = map(json.loads, screen.graded_path(out).read_text().splitlines())
    return {(g["model"], g["question"], g["repeat"]): g for g in rows}


def export_and_judge(tmp_path: Path, qdir: Path, tree: Path, out: Path, model: str) -> Path:
    answers = tmp_path / "answers.jsonl"
    assert open_screen.main(["export-answers", str(out), "--answers", str(answers)]) == 0
    argv = ["judge", "--answers", str(answers), "--split", "screen", "--model", model]
    assert open_judge.main([*argv, "--dir", str(qdir), "--tree", str(tree)]) == 0
    return answers


def run_report(out: Path, tree: Path, judgments: Path, judge: str) -> str:
    argv = ["report", str(out), "--judgments", str(judgments), "--tree", str(tree)]
    assert open_screen.main([*argv, "--judge-model", judge]) == 0
    return ""


def test_runner_rows_have_the_real_field_list() -> None:
    assert len(RUNNER_FIELDS) == 58 and "experiment_id" in RUNNER_FIELDS
    row = runner_row(arm="a")
    assert set(row) == set(RUNNER_FIELDS) and row["seed"] is None and row["deviations"] == []


def test_every_trial_is_scored_through_export_judge_and_report_with_the_expected_numbers(
    tmp_path: Path, tree: Path, qdir: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    stub = Stub()
    judgments = patch_judge(tmp_path, monkeypatch, stub)
    out = runner_results(tmp_path, "screen", "screen-clean.jsonl")
    export_and_judge(tmp_path, qdir, tree, out, JA)
    assert stub.calls == [JA] * 4  # four distinct answers; the missing one needs no judge
    capsys.readouterr()
    run_report(out, tree, judgments, JA)
    text = capsys.readouterr().out
    got = graded_by_trial(out)
    assert len(got) == 6 and "unjudged" not in {g["status"] for g in got.values()}
    want = {  # (model, question, repeat): (status, recall, citation_valid, silent_wrong)
        (SMALL, "T-1", 1): ("graded", 1.0, 1.0, False),
        (BIG, "T-1", 1): ("graded", 1.0, 1.0, False),
        (SMALL, "N-1", 1): ("graded", 1.0, 1.0, False),
        (SMALL, "E-1", 1): ("graded", 0.5, 1.0, False),
        (BIG, "E-1", 1): ("no_answer", 0.0, 0.0, False),
        (SMALL, "T-1", 2): ("graded", 1.0, 0.0, True),  # a fabricated citation, asserted
    }
    for key, (status, recall, cite, silent) in want.items():
        g = got[key]
        assert (g["status"], g["recall"], g["citation_valid"], g["silent_wrong"]) == (
            status, recall, cite, silent
        ), key  # fmt: skip
    assert got[(SMALL, "T-1", 2)]["fabrications"] == 1
    assert "| qwen3.6:27b-64k | all | 4 | 0 | 0 |" in text
    assert "| laguna-xs-2.1:q4_K_M-64k | all | 2 | 1 | 0 |" in text  # one missing, one excluded
    assert "| laguna-xs-2.1:q4_K_M-64k | N-1 | 1 | timeout |" in text
    assert "1/4 (<= " in text  # silent_wrong over the small model's four answered trials
    # the second judge scores the same answers its own way, from the same file
    export_and_judge(tmp_path, qdir, tree, out, JB)
    assert stub.calls[4:] == [JB] * 4
    capsys.readouterr()
    run_report(out, tree, judgments, JB)
    other = graded_by_trial(out)
    assert other[(SMALL, "T-1", 1)]["recall"] == 0.5 and other[(SMALL, "E-1", 1)]["recall"] == 0.5
    assert other[(SMALL, "N-1", 1)]["recall"] == 1.0
    ghost = other[(SMALL, "T-1", 2)]  # not asserted by B: no counted citation, not silent_wrong
    assert (ghost["silent_wrong"], ghost["citations_total"], ghost["recall"]) == (False, 0, 0.5)
    # a judge that judged nothing scores nothing
    run_report(out, tree, judgments, "nobody")
    assert {g["status"] for g in graded_by_trial(out).values()} == {"unjudged", "no_answer"}


def test_a_rerun_of_the_judge_spends_nothing_and_a_row_of_an_older_template_is_not_scored(
    tmp_path: Path, tree: Path, qdir: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    stub = Stub()
    judgments = patch_judge(tmp_path, monkeypatch, stub)
    out = runner_results(tmp_path, "screen", "screen-clean.jsonl")
    export_and_judge(tmp_path, qdir, tree, out, JA)
    export_and_judge(tmp_path, qdir, tree, out, JA)
    assert len(stub.calls) == 4  # the rerun came from the cache
    # the judge's template changes: its rows are stale and no trial is scored from them
    monkeypatch.setattr(open_judge, "SYSTEM", open_judge.SYSTEM + " Be brief.")
    capsys.readouterr()
    run_report(out, tree, judgments, JA)
    text = capsys.readouterr().out
    got = graded_by_trial(out)
    assert {g["status"] for g in got.values()} == {"unjudged", "no_answer"}
    assert sum(g["status"] == "unjudged" for g in got.values()) == 5
    assert text.count("stale judgment: written under an older template") == 5
    assert not [g for g in got.values() if g["status"] == "unjudged" and "recall" in g]
    export_and_judge(tmp_path, qdir, tree, out, JA)  # judging again under the new template
    assert len(stub.calls) == 8
    run_report(out, tree, judgments, JA)
    assert "unjudged" not in {g["status"] for g in graded_by_trial(out).values()}


def test_the_audit_runs_both_judges_on_an_export_of_the_dev_split(
    tmp_path: Path, tree: Path, qdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = Stub()
    patch_judge(tmp_path, monkeypatch, stub)
    out = runner_results(tmp_path, "dev", "dev-clean.jsonl")
    answers = tmp_path / "answers.jsonl"
    assert open_screen.main(["export-answers", str(out), "--answers", str(answers)]) == 0
    report_path = tmp_path / "audit.md"
    argv = ["audit", "--answers", str(answers), "--dir", str(qdir), "--tree", str(tree)]
    assert open_judge.main([*argv, "--out", str(report_path)]) == 1  # 9/12 agree: below 0.85
    assert sorted(stub.calls) == sorted([JA, JB] * 4)
    text = report_path.read_text()
    assert "Verdict: FAIL" in text and "Failed judgments: 0" in text
    for line in ("- fact: 5/7", "- claim: 4/4", "- candidate: 0/1", "- overall: 9/12 = 0.750"):
        assert line in text, line
    assert "## Disagreements (3), every one" in text
    assert f"| {JA} | 4 | 0.0400 | 0.0100 | 1.20 |" in text
