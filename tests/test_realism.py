"""Tests for the comparison, the reference runner and the long-task study, on synthetic data."""

from __future__ import annotations

import dataclasses
import json
import math
import shutil
import signal
from pathlib import Path
from typing import Any

import pytest

from bench import compare, layout, preflight, realism, reference, runner, sandbox, stats
from bench.transcript import Transcript, sanitize_transcript
from tests.helpers import git, needs_fixture_files
from tests.helpers import needs_bwrap as needs_bwrap_marker

CONFIG = {
    "model": "ollama/m",
    "provider": {"ollama": {"models": {}}},
    "agent": {"a": {"model": "ollama/m"}},
}


def _events(tools: list[tuple[str, dict[str, Any], str, str]], text: str) -> str:
    """Builds a JSON-lines transcript of tool calls with status and error, then text and finish."""
    lines = []
    for tool, inp, status, err in tools:
        state = {"status": status, "input": inp, "error": err, "output": err}
        lines.append(json.dumps({"type": "tool_use", "part": {"tool": tool, "state": state}}))
    lines.append(json.dumps({"type": "text", "part": {"text": text}}))
    lines.append(json.dumps({"type": "step_finish", "part": {"tokens": {"total": 5}}}))
    return "\n".join(lines) + "\n"


def _record(tmp_path: Path, env: str, i: int, events: str) -> dict[str, Any]:
    """Writes a transcript to a new dir and returns a trial record for the given side and index."""
    adir = tmp_path / f"{env}-{i}"
    adir.mkdir()
    (adir / "transcript.jsonl").write_text(events)
    tr = Transcript().parse(events)
    return {
        "label": "t1",
        "environment": env,
        "outcome": "completed",
        "secs": 10.0 + i,
        "tool_calls": len(tr.tools),
        "tool_errors": tr.tool_errors,
        "artifact": str(adir),
    }


def test_compare_finds_a_one_sided_error_and_normalises_paths(tmp_path: Path) -> None:
    """A tool error seen only on the fixture side is reported, and workdir paths are stripped."""
    good = _events([("glob", {}, "completed", "")], f"{sandbox.WORKDIR}/utils/x.py:3")
    broken = _events(
        [("glob", {}, "error", "ripgrep execution failed")], f"{sandbox.WORKDIR}/utils/x.py:3"
    )
    records = [
        _record(tmp_path, "fixture", 1, broken),
        _record(tmp_path, "fixture", 2, broken),
        _record(tmp_path, "reference", 1, good),
        _record(tmp_path, "reference", 2, good),
    ]
    report = compare.compare(records)["t1"]
    assert (
        report["fixture"]["tool_error_trials"] == 2
        and report["reference"]["tool_error_trials"] == 0
    )
    assert report["errors_only_fixture"] == ["glob -> ripgrep execution failed"]
    assert report["errors_only_reference"] == []
    assert report["fixture"]["answers"] == report["reference"]["answers"] == ["utils/x.py:3"] * 2
    md = compare.markdown(compare.compare(records))
    assert "errors only in the fixture" in md and "ripgrep execution failed" in md


def test_command_head_reduces_bash_and_keeps_other_tools() -> None:
    """`command_head` reduces a bash call to its leading command and returns other tools by name."""
    bash = {
        "tool": "bash",
        "input": {"command": f'bash -lc "cd {sandbox.WORKDIR} && git status --porcelain"'},
    }
    assert compare.command_head(bash) == f"bash: cd {sandbox.WORKDIR}"
    assert compare.command_head({"tool": "read", "input": {}}) == "read"


def _git_source(tmp_path: Path) -> Path:
    """Creates a one-file git repo with a single commit and returns its path."""
    source = tmp_path / "src"
    (source / "pkg").mkdir(parents=True)
    (source / "pkg" / "m.py").write_text("X = 1\n")
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "base"]):
        git(source, *args)
    return source


EVENT = '{"type":"text","part":{"text":"hi"}}'
needs_bwrap = needs_bwrap_marker


def _reference(tmp_path: Path, source: Path, script: str, spec: runner.TrialSpec) -> dict[str, Any]:
    """Runs the reference runner on a source repo with a fake agent script; returns its record."""
    cfg = tmp_path / "real.json"
    cfg.write_text(json.dumps(CONFIG))
    return reference.run_reference(
        spec,
        source,
        tmp_path / "artifacts",
        tmp_path / "trials",
        tmp_path / "r.jsonl",
        agent_argv=["sh", "-c", script],
        config_source=cfg,
        check=lambda force=False: [],
        venv=None,
    )


@needs_bwrap
def test_reference_runner_uses_a_disposable_copy_and_records_the_same_shape(tmp_path: Path) -> None:
    """A reference run edits a disposable copy, records the usual shape and spares the source."""
    source = _git_source(tmp_path)
    hooks = (runner.Hook("dirty", "pkg/m.py", "# dirt\n"),)
    spec = runner.TrialSpec(agent="a", model="m", prompt="p", label="t1", hooks=hooks, net="none")
    script = f"echo '{EVENT}'; echo new > {sandbox.WORKDIR}/added.txt"
    rec = _reference(tmp_path, source, script, spec)
    assert rec["environment"] == "reference" and rec["label"] == "t1"
    assert rec["trial_id"] == f"ref-{spec.trial_id}" and rec["outcome"] == "completed"
    adir = Path(rec["artifact"])
    assert "added.txt" not in (adir / "status.txt").read_text(), "real-code paths are not kept"
    assert (adir / "diff.patch").read_text() == ""
    assert (source / "pkg" / "m.py").read_text() == "X = 1\n", "the source tree must stay untouched"
    assert not any((tmp_path / "trials").iterdir()), "the disposable copy must be deleted"


@needs_bwrap
def test_the_reference_agent_is_sandboxed_like_a_fixture_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the reference once ran with the host visible (keys, network, host-side git)."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sekret-value-xyz")
    source = _git_source(tmp_path)
    marker = tmp_path / "host-marker"
    script = (
        "pwd; env; ls /home; ls -d /home/*/.ssh 2>&1; "
        f"echo x > {tmp_path}/leak.txt 2>/dev/null; echo leak_rc=$?; "
        # a hostile .git/config: the read-back must not run it on the host
        f"git -C {sandbox.WORKDIR} config core.fsmonitor 'touch {marker}'; "
        f"echo x >> {sandbox.WORKDIR}/pkg/m.py"
    )
    spec = runner.TrialSpec(agent="a", model="m", prompt="p", label="t1", net="none")
    rec = _reference(tmp_path, source, script, spec)
    out = (Path(rec["artifact"]) / "transcript.jsonl").read_text()
    assert out.split()[0] == sandbox.WORKDIR, "the agent works at the real path, in its copy"
    assert "sekret-value-xyz" not in out and "OPENROUTER" not in out, "host env leaked"
    assert "/.ssh" not in out.replace("ls: cannot access '/home/*/.ssh': No such file", ""), (
        "the host's home directory is visible"
    )
    assert "leak_rc=0" not in out and not (tmp_path / "leak.txt").exists()
    assert not marker.exists(), "a command planted in .git/config ran on the host"
    assert (source / "pkg" / "m.py").read_text() == "X = 1\n"


@needs_bwrap
def test_a_killed_or_failed_reference_trial_still_leaves_no_copy_behind(tmp_path: Path) -> None:
    """A reference trial whose pre-run check raises propagates the error and leaves no copy."""
    source = _git_source(tmp_path)
    spec = runner.TrialSpec(agent="a", model="m", prompt="p", net="none")

    def blocked(force: bool = False) -> list[Any]:
        """Stands in for the pre-run check and always raises `RuntimeError`."""
        raise RuntimeError("blocked")

    cfg = tmp_path / "real.json"
    cfg.write_text(json.dumps(CONFIG))
    with pytest.raises(RuntimeError):
        reference.run_reference(
            spec,
            source,
            tmp_path / "a",
            tmp_path / "trials",
            tmp_path / "r.jsonl",
            config_source=cfg,
            check=blocked,
            venv=None,
        )
    assert sorted((tmp_path / "trials").glob("**/*")) == [], "nothing is left of a blocked run"


def test_discard_removes_a_stale_prepared_copy_including_unreadable_dirs(tmp_path: Path) -> None:
    """`discard` removes a stale prepared copy with an unreadable subdir; an absent one is fine."""
    stale = tmp_path / "ref-source"
    locked = stale / "tree" / "locked"
    locked.mkdir(parents=True)
    (locked / "f").write_text("x")
    locked.chmod(0)
    reference.discard(stale)
    assert not stale.exists()
    reference.discard(stale)  # absent is fine


def _rec(
    side: str, label: str, calls: int, secs: float, ok: bool = True, err: int = 0
) -> dict[str, Any]:
    """Builds a minimal trial record for a side and task with calls, seconds, outcome and errors."""
    return {
        "environment": side,
        "label": label,
        "tool_calls": calls,
        "secs": secs,
        "outcome": "completed" if ok else "timeout",
        "tool_errors": err,
    }


def test_effects_call_equal_sides_equivalent_and_a_2x_gap_different() -> None:
    """`effects` calls equal sides equivalent and a 2x gap in tool calls different."""
    same = [
        _rec(s, f"t{i}", 4 + (i % 2), 10.0) for i in range(8) for s in ("fixture", "reference")
    ] * 2
    eff = compare.effects(same)
    assert eff["tool_calls"]["verdict"] == "equivalent" and eff["secs"]["verdict"] == "equivalent"
    assert eff["completed"]["fixture"]["k"] == eff["completed"]["fixture"]["n"] == 16
    gap = [_rec("fixture", f"t{i}", 4, 10.0) for i in range(8) for _ in range(3)]
    gap += [_rec("reference", f"t{i}", 8 + i % 2, 10.0) for i in range(8) for _ in range(3)]
    worse = compare.effects(gap)
    assert worse["tool_calls"]["verdict"] == "differs" and worse["tool_calls"]["ratio"] < 0.6
    assert "differs" in compare.effects_markdown(worse)


def test_effects_with_no_shared_tasks_say_no_data() -> None:
    """`effects` gives the verdict no data when no task appears on both sides."""
    eff = compare.effects([_rec("fixture", "a", 3, 1.0), _rec("reference", "b", 3, 1.0)])
    assert eff["tool_calls"]["verdict"] == "no data"


def test_paired_effect_reports_a_consistent_per_task_gap_the_pooled_ratio_can_hide() -> None:
    """Regression: a ratio of sums, dominated by one slow task, hid a gap present on every task."""
    records = []
    for i in range(10):
        slow = 100.0 if i == 0 else 10.0
        records += [_rec("fixture", f"t{i}", 3, slow * 1.5) for _ in range(2)]
        records += [_rec("reference", f"t{i}", 3, slow) for _ in range(2)]
    paired = compare.effects(records)["secs"]["paired"]
    assert paired["tasks"] == 10 and paired["higher"] == 10 and paired["lower"] == 0
    assert abs(paired["geomean"] - 1.5) < 1e-9 and paired["sign_p"] < 0.01
    assert "per task" in compare.effects_markdown(compare.effects(records))


@needs_bwrap
def test_sweep_removes_every_stale_reference_copy_and_leaves_other_trials(tmp_path: Path) -> None:
    """`sweep` removes every stale reference copy, even unreadable ones, and keeps other dirs."""
    for name in ("ref-source", "ref-abc123"):
        locked = tmp_path / name / "work" / "locked"
        locked.mkdir(parents=True)
        (locked / "f").write_text("real code")
        locked.chmod(0)
    (tmp_path / "keep").mkdir()
    reference.sweep(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["keep"]
    reference.sweep(tmp_path / "absent")


def test_a_reference_transcript_keeps_the_shape_of_the_run_and_none_of_the_code() -> None:
    """Sanitising keeps the transcript shape but swaps code for lengths and truncates the rest."""
    long_code = "def secret_strategy():\n" + "    x = 1\n" * 500
    state = {
        "status": "completed",
        "input": {"filePath": "a.py", "content": long_code, "command": "ls -la " + "x" * 100},
        "output": long_code,
        "metadata": {"preview": long_code},
    }
    events = [
        {"type": "tool_use", "part": {"tool": "read", "state": state}},
        {"type": "text", "part": {"text": long_code}},
    ]
    raw = "\n".join(json.dumps(e) for e in events) + "\nplain stdout line\n"
    out = sanitize_transcript(raw)
    assert len(out) < 1200 and "secret_strategy" not in out.splitlines()[0]
    tool = json.loads(out.splitlines()[0])["part"]
    assert tool["tool"] == "read" and tool["state"]["status"] == "completed"
    assert tool["state"]["input"]["filePath"] == "a.py"
    assert tool["state"]["input"]["content"] == f"<{len(long_code)} chars>"
    assert len(tool["state"]["input"]["command"]) == 40
    assert tool["state"]["output"] == "" and "metadata" not in tool["state"]
    assert len(json.loads(out.splitlines()[1])["part"]["text"]) == 160
    assert out.splitlines()[-1] == "plain stdout line"


def test_tool_output_survives_only_as_a_not_found_flag() -> None:
    """Tool output survives sanitising only as the flag `not found`, when the output says so."""
    event = {"type": "tool_use", "part": {"tool": "bash", "state": {"output": "sh: x: not found"}}}
    assert json.loads(sanitize_transcript(json.dumps(event)))["part"]["state"]["output"] == (
        "not found"
    )


def test_prepare_pins_the_copy_once_so_later_drift_is_visible(tmp_path: Path) -> None:
    """A pinned reference copy passes the fixture check; a later file edit raises `DriftError`."""
    source = _git_source(tmp_path)
    dest = tmp_path / "ref"
    tree = dest / "tree"
    shutil.copytree(source, tree)
    manifest = reference._manifest(tree, "abc")
    (dest / "manifest.json").write_text(json.dumps(manifest))
    clean = reference.fixture(tree)
    runner.check_fixture(clean)
    (tree / "pkg" / "m.py").write_text("X = 99\\n")
    with pytest.raises(runner.DriftError):
        runner.check_fixture(reference.fixture(tree))


def test_sigterm_during_a_reference_run_still_runs_the_cleanup() -> None:
    """SIGTERM in `cleanup_on_signals` still runs the cleanup, then exits 128 plus the signal."""
    cleaned = []
    with pytest.raises(SystemExit) as exc:
        with reference.cleanup_on_signals():
            try:
                signal.raise_signal(signal.SIGTERM)
            finally:
                cleaned.append(True)
    assert exc.value.code == 128 + signal.SIGTERM and cleaned == [True]


def test_a_reference_transcript_is_a_whitelist_not_a_blacklist() -> None:
    """Regression: `raw` tool arguments, long errors and unknown fields survived the sanitiser."""
    code = "def secret():\n" + "    pass\n" * 300
    event = {
        "type": "tool_use",
        "sessionID": "s",
        "provider_payload": code,
        "part": {
            "tool": "edit",
            "raw": code,
            "state": {
                "status": "error",
                "input": {"filePath": "a.py", "newString": code, "nested": [code]},
                "error": code,
                "raw": code,
            },
        },
    }
    out = sanitize_transcript(json.dumps(event))
    assert len(out) < 1000 and "provider_payload" not in out and '"raw"' not in out
    kept = json.loads(out)["part"]["state"]
    assert kept["input"]["newString"] == f"<{len(code)} chars>" and len(kept["error"]) == 60


def test_a_reference_trial_keeps_no_diff_status_or_repo_state(tmp_path: Path) -> None:
    """With redaction, repo artifacts hold an empty diff, no file names or code, a change count."""
    run = runner._Run(status=" M real.py\n", diff="+ secret code\n", changes={"written": ["a"]})
    runner._write_repo_artifacts(tmp_path, run, redact=True)
    assert (tmp_path / "diff.patch").read_text() == ""
    assert "real.py" not in (tmp_path / "status.txt").read_text()
    assert "secret" not in (tmp_path / "git_state.json").read_text()
    assert json.loads((tmp_path / "changes.json").read_text())["written"] == 1


@needs_bwrap
def test_a_reference_record_keeps_the_kind_of_failure_but_not_its_text(tmp_path: Path) -> None:
    """The detail of a harness error can hold git or hook stderr from the real repo."""
    source = _git_source(tmp_path)
    hooks = (runner.Hook("dirty", "no/such/dir/secret_module.py", "# x\n"),)
    spec = runner.TrialSpec(agent="a", model="m", prompt="p", net="none", hooks=hooks)
    rec = _reference(tmp_path, source, "exit 0", spec)
    assert rec["outcome"] == "harness_error"
    assert rec["detail"] == "RuntimeError"


# --- the long-task study (G4): tasks, order, resume, measures, ratios, verdicts, orchestration ---


def test_the_long_tasks_are_eight_distinct_fix_and_commit_prompts() -> None:
    """Eight distinct tasks on one agent and model, each asking for a check and a commit."""
    tasks = realism.LONG_TASKS
    assert len(tasks) == 8 and len({t.label for t in tasks}) == 8
    assert len({t.prompt for t in tasks}) == 8
    assert {(t.agent, t.model) for t in tasks} == {("full", "devstral-small-2:24b")}
    for t in tasks:
        assert "commit" in t.prompt and t.hooks == (), t.label
        assert "ruff" in t.prompt, f"{t.label}: no check is asked for before the commit"


@needs_fixture_files
def test_every_path_a_long_task_names_is_in_the_fixture_tree() -> None:
    """A typo in a prompt's file path would send the agent on a task that cannot be done."""
    tree = layout.tree()
    for t in realism.LONG_TASKS:
        named = [
            w.strip("`.,") for w in t.prompt.split() if "/" in w and w.strip("`.,").endswith(".py")
        ]
        assert named, f"{t.label} names no file"
        for path in named:
            assert (tree / path).is_file(), f"{t.label}: {path} is not in the fixture tree"


needs_plan = pytest.mark.skipif(
    not realism.PLAN.exists(), reason="the plan is not present (it stays in the private repo)"
)


@needs_plan
def test_the_plan_names_the_fingerprint_band_seed_and_size_the_code_runs() -> None:
    """The plan and the code agree on the tasks, the band, the seed, the size and the limits."""
    text = realism.PLAN.read_text()
    assert f"tasks_sha256: {realism.tasks_fingerprint()}" in text, "tasks changed: update the plan"
    assert f"{realism.BAND[0]} to {realism.BAND[1]}" in text
    assert f"Seed {realism.LONG_SEED}" in text
    trials = len(realism.LONG_TASKS) * len(realism.LONG_SIDES) * realism.LONG_REPEATS
    assert f"**{trials} trials**" in text
    assert str(realism.LONG_TIMEOUT) in text and str(realism.LONG_HANG) in text


def test_long_cells_cover_each_task_side_and_repeat_once_in_a_seeded_order() -> None:
    """Every cell appears once, in a replayable order, balanced by repeat and extendable by n."""
    cells = realism.long_cells(13, 4)
    assert len(cells) == len(set(cells)) == 8 * 2 * 4
    assert {(c.label, c.side) for c in cells} == {
        (t.label, s) for t in realism.LONG_TASKS for s in ("fixture", "reference")
    }
    assert [c.rep for c in cells] == sorted(c.rep for c in cells), "a partial run is balanced"
    assert cells == realism.long_cells(13, 4), "the order replays from its seed"
    assert [(c.label, c.side) for c in cells[:16]] != [(c.label, c.side) for c in cells[16:32]]
    assert cells != realism.long_cells(14, 4)
    first_block = cells[:16]
    assert {c.side for c in first_block[:8]} == {"fixture", "reference"}, "sides are interleaved"
    assert realism.long_cells(13, 2) == cells[:32], "a smaller n is a prefix, so n can be extended"


def _row(cell: realism.Cell, outcome: str = "completed") -> dict[str, Any]:
    """A minimal record of one cell, as the runner would append it."""
    return {
        "label": cell.label,
        "environment": cell.side,
        "repeat": cell.rep + 1,
        "outcome": outcome,
    }


def test_remaining_cells_skip_what_is_recorded_but_redo_a_harness_error() -> None:
    """A recorded result stays done (a timeout included); a harness error is a gap to redo."""
    cells = realism.long_cells(13, 1)
    done = [_row(cells[0]), _row(cells[1], "timeout"), _row(cells[2], "harness_error")]
    left = realism.remaining(cells, done)
    assert cells[0] not in left and cells[1] not in left, "a timeout is a result, not a gap"
    assert cells[2] in left and len(left) == 14, "a harness error is never a model result"
    assert realism.remaining(cells, [_row(c) for c in cells]) == []


def runner_task_with(task: realism.Task, **changes: Any) -> realism.Task:
    """`task` with some fields replaced."""
    return dataclasses.replace(task, **changes)


def test_the_task_fingerprint_changes_with_any_prompt_or_setting() -> None:
    """Editing a prompt or the model changes the fingerprint the plan has to name."""
    base = realism.tasks_fingerprint()
    assert base == realism.tasks_fingerprint() and len(base) == 16
    edited = (
        *realism.LONG_TASKS[:-1],
        runner_task_with(
            realism.LONG_TASKS[-1], prompt=realism.LONG_TASKS[-1].prompt + " Be quick."
        ),
    )
    assert realism.tasks_fingerprint(edited) != base
    other_model = (
        *realism.LONG_TASKS[:-1],
        runner_task_with(realism.LONG_TASKS[-1], model="gpt-oss:20b"),
    )
    assert realism.tasks_fingerprint(other_model) != base


def _repo_with_plan(tmp_path: Path, text: str, *, commit: bool = True) -> tuple[Path, Path]:
    """A git repo holding one plan file, committed or not."""
    repo = tmp_path / "repo"
    (repo / "plans").mkdir(parents=True)
    plan = repo / "plans" / "PLAN.md"
    plan.write_text(text)
    git(repo, "init", "-q", "-b", "main")
    if commit:
        git(repo, "add", "plans/PLAN.md")
        git(repo, "commit", "-qm", "plan")
    return repo, plan


def test_a_run_needs_a_committed_clean_plan_that_names_its_tasks(tmp_path: Path) -> None:
    """A missing, uncommitted, edited or mismatched plan stops the run before any trial."""
    fp = realism.tasks_fingerprint()
    repo, plan = _repo_with_plan(tmp_path, f"tasks_sha256: {fp}\n")
    head = git(repo, "rev-parse", "HEAD")
    assert realism.require_preregistered(plan, fp, repo) == head

    plan.write_text(f"tasks_sha256: {fp}\nedited after the commit\n")
    with pytest.raises(realism.PreregistrationError, match="uncommitted"):
        realism.require_preregistered(plan, fp, repo)

    repo2, plan2 = _repo_with_plan(tmp_path / "b", f"tasks_sha256: {fp}\n", commit=False)
    with pytest.raises(realism.PreregistrationError, match="not committed"):
        realism.require_preregistered(plan2, fp, repo2)

    repo3, plan3 = _repo_with_plan(tmp_path / "c", "tasks_sha256: 0000000000000000\n")
    with pytest.raises(realism.PreregistrationError, match="fingerprint"):
        realism.require_preregistered(plan3, fp, repo3)
    with pytest.raises(realism.PreregistrationError, match="no plan"):
        realism.require_preregistered(tmp_path / "missing.md", fp, repo3)


def test_the_manifest_pins_seed_and_tasks_and_lets_n_only_grow(tmp_path: Path) -> None:
    """A resume keeps the seed and tasks; a second look may only add repeats, and is recorded."""
    path = tmp_path / "long.manifest.json"
    facts = {"seed": 13, "n": 4, "tasks_sha256": "abc", "plan_commit": "deadbeef"}
    realism.check_manifest(path, facts)
    assert json.loads(path.read_text()) == {**facts, "looks": [4]}
    realism.check_manifest(path, facts)  # the same run resumes
    assert json.loads(path.read_text())["looks"] == [4], "a resume is not a new look"
    for key, value in (("seed", 14), ("tasks_sha256", "xyz")):
        with pytest.raises(ValueError, match=key):
            realism.check_manifest(path, {**facts, key: value})
    realism.check_manifest(path, {**facts, "n": 8})  # a second look adds repeats
    saved = json.loads(path.read_text())
    assert saved["n"] == 8 and saved["looks"] == [4, 8]
    with pytest.raises(ValueError, match="n can only grow"):
        realism.check_manifest(path, {**facts, "n": 4})


def _long_events(tools: list[tuple[str, dict[str, Any], str]], final: str, steps: int) -> str:
    """A JSON-lines transcript of the given tool calls, a final answer and `steps` step events."""
    lines = []
    for tool, inp, status in tools:
        state = {
            "status": status,
            "input": inp,
            "error": "boom" if status == "error" else "",
            "output": "",
        }
        lines.append(json.dumps({"type": "tool_use", "part": {"tool": tool, "state": state}}))
    lines.append(json.dumps({"type": "text", "part": {"text": final}}))
    for _ in range(steps):
        tokens = {"total": 10, "input": 7, "output": 3}
        lines.append(json.dumps({"type": "step_finish", "part": {"tokens": tokens}}))
    return "\n".join(lines) + "\n"


def _long_trial(
    tmp_path: Path,
    side: str,
    label: str,
    rep: int,
    tools: list[tuple[str, dict[str, Any], str]] | None = None,
    *,
    diff: str = "+x\n" * 10,
    outcome: str = "completed",
    secs: float = 60.0,
    steps: int = 3,
    final: str = "Done.",
) -> dict[str, Any]:
    """A recorded trial and its artifacts, written the way the runner writes them for that side."""
    tools = tools if tools is not None else [("read", {"filePath": "a.py"}, "completed")]
    raw = _long_events(tools, final, steps)
    adir = tmp_path / f"{side}-{label}-{rep}"
    adir.mkdir()
    tr = Transcript().parse(raw)
    if side == "reference":
        (adir / "transcript.jsonl").write_text(sanitize_transcript(raw))
        (adir / "status.txt").write_text("2 lines omitted\n")
        (adir / "diff.patch").write_text("")
        (adir / "git_state.json").write_text(json.dumps({"omitted": "x", "diff_chars": len(diff)}))
    else:
        (adir / "transcript.jsonl").write_text(raw)
        (adir / "status.txt").write_text(" M a.py\n M b.py\n")
        (adir / "diff.patch").write_text(diff)
        (adir / "git_state.json").write_text(
            json.dumps({"commits": [], "staged": [], "stashes": []})
        )
    return {
        "label": label,
        "environment": side,
        "repeat": rep + 1,
        "outcome": outcome,
        "answer_kind": tr.answer_kind,
        "secs": secs,
        "tool_calls": tr.tool_count,
        "tool_errors": tr.tool_errors,
        "steps": tr.steps,
        "tokens_in": tr.tokens_in,
        "tokens_out": tr.tokens_out,
        "written_files": 4,
        "artifact": str(adir),
    }


MIXED_TOOLS: list[tuple[str, dict[str, Any], str]] = [
    ("read", {"filePath": "a.py"}, "completed"),
    ("edit", {"filePath": "a.py", "oldString": "x", "newString": "yy"}, "completed"),
    ("edit", {"filePath": "b.py", "oldString": "x", "newString": "yy"}, "error"),
    ("write", {"filePath": "tests/t.py", "content": "def test(): pass\n"}, "completed"),
    ("bash", {"command": "git commit -m 'fix: x'"}, "completed"),
    (
        "bash",
        {"command": "cd /mnt/ParamoStorage/Paramo && git add a.py && git commit -m x"},
        "completed",
    ),
]


def test_measures_read_the_same_from_a_fixture_trial_and_its_sanitised_reference_twin(
    tmp_path: Path,
) -> None:
    """Both sides are read through the sanitised transcript, so a trial has one set of measures."""
    fx = realism.measures(_long_trial(tmp_path, "fixture", "t", 0, MIXED_TOOLS, steps=4))
    ref = realism.measures(_long_trial(tmp_path, "reference", "t", 0, MIXED_TOOLS, steps=4))
    assert fx == ref, "both sides are read through the same lens"
    assert fx["tool_calls"] == 6 and fx["steps"] == 4 and fx["tool_error_trial"] == 1.0
    assert fx["edit_calls"] == 2, "an errored edit is not a change"
    assert fx["files_edited"] == 2, "a.py and tests/t.py"
    assert fx["commit_calls"] == 1, "a commit hidden past the 40-character head is not seen"
    assert fx["diff_chars"] == 30 and fx["status_lines"] == 2
    assert "written_files" not in fx, "it counts the tree (2,268 vs 2,796 files for a no-op trial)"


def test_done_needs_a_clean_finish_an_answer_and_a_changed_repo(tmp_path: Path) -> None:
    """`done` is 0 for a timeout, an empty or tool-call answer, or a repo left unchanged."""

    def done(**kw: Any) -> float:
        """The `done` measure of a fresh trial of the given side and options."""
        return realism.measures(
            _long_trial(
                tmp_path, kw.pop("side", "fixture"), "t", len(list(tmp_path.iterdir())), **kw
            )
        )["done"]

    assert done() == 1.0 and done(side="reference") == 1.0
    assert done(diff="") == 0.0 and done(side="reference", diff="") == 0.0
    assert done(outcome="timeout") == 0.0
    assert done(final='{"name": "bash", "arguments": {}}') == 0.0, "a tool call is not an answer"
    assert done(final="") == 0.0


def test_geomean_ratio_gives_each_task_one_vote_where_the_pooled_ratio_follows_the_big_one() -> (
    None
):
    """One long task cannot hide a 1.5x gap on the other seven."""
    num = {"big": [100.0] * 4, **{f"t{i}": [15.0] * 4 for i in range(7)}}
    den = {"big": [100.0] * 4, **{f"t{i}": [10.0] * 4 for i in range(7)}}
    point, lo, hi = realism.geomean_ratio(num, den)
    assert abs(point - 1.5 ** (7 / 8)) < 1e-9 and lo <= point <= hi
    pooled = stats.bootstrap_ratio(num, den)[0]
    assert point > pooled + 0.15, "the pooled ratio hid a 1.5x gap on seven of eight tasks"
    assert realism.geomean_ratio(num, den) == realism.geomean_ratio(num, den), "seeded"


def test_geomean_ratio_of_equal_sides_is_one_and_a_twofold_gap_is_two() -> None:
    """Equal sides give exactly 1 with an interval around it; a doubled side gives 2."""
    same = {f"t{i}": [4.0, 6.0, 5.0, 5.0] for i in range(8)}
    point, lo, hi = realism.geomean_ratio(same, same)
    assert point == 1.0 and lo < 1.0 < hi
    doubled = {k: [2 * x for x in v] for k, v in same.items()}
    assert abs(realism.geomean_ratio(doubled, same)[0] - 2.0) < 1e-9


def test_geomean_ratio_with_a_zero_task_mean_says_no_data_instead_of_a_number() -> None:
    """A task with a zero mean (or no tasks) yields NaN, which the verdict reports as no data."""
    num = {"a": [3.0, 3.0], "b": [0.0, 0.0]}
    den = {"a": [3.0, 3.0], "b": [2.0, 2.0]}
    assert all(math.isnan(x) for x in realism.geomean_ratio(num, den))
    assert all(math.isnan(x) for x in realism.geomean_ratio({}, {}))


def test_rate_ratio_of_two_clean_sides_is_inside_the_band_and_a_halved_rate_is_not() -> None:
    """Eight tasks that always succeed on both sides are equivalent; half the successes differs."""
    clean = {f"t{i}": [True] * 4 for i in range(8)}
    point, lo, hi = realism.rate_ratio(clean, clean)
    assert point == 1.0 and 0.8 <= lo and hi <= 1.25
    half = {f"t{i}": [True, True, False, False] for i in range(8)}
    point, lo, hi = realism.rate_ratio(clean, half)
    assert point == 2.0 and realism.verdict(point, lo, hi) == "differs"


@pytest.mark.parametrize(
    ("ratio", "lo", "hi", "expected"),
    [
        (1.0, 0.85, 1.2, "equivalent"),
        (1.0, 0.8, 1.25, "equivalent"),  # the band's own edges count as inside
        (1.0, 0.7, 1.2, "inconclusive"),  # the interval leaves the band: the study cannot say
        (1.05, 0.99, 1.4, "inconclusive"),
        (1.5, 1.3, 1.9, "differs"),  # wholly above the band
        (0.6, 0.5, 0.75, "differs"),  # wholly below it
        (1.2, 1.1, 1.4, "inconclusive"),  # excludes 1 yet overlaps the band: not a difference
        (float("nan"), float("nan"), float("nan"), "no data"),
    ],
)
def test_verdict_decides_only_when_the_whole_interval_is_in_or_out_of_the_band(
    ratio: float, lo: float, hi: float, expected: str
) -> None:
    """Equivalent, differs, inconclusive and no data at the band's edges and between them."""
    assert realism.verdict(ratio, lo, hi) == expected


def _study(
    tmp_path: Path, n: int, fixture_calls: int, reference_calls: int
) -> list[dict[str, Any]]:
    """Records of the eight tasks on both sides for `n` repeats, with the given calls per trial."""
    rows = []
    for t in realism.LONG_TASKS:
        for rep in range(n):
            for side, calls in (("fixture", fixture_calls), ("reference", reference_calls)):
                tools = [("read", {"filePath": "a.py"}, "completed")] * (calls + rep % 2)
                rows.append(_long_trial(tmp_path, side, t.label, rep, tools, steps=calls + rep % 2))
    return rows


def test_the_report_calls_a_matched_study_equivalent_and_holds(tmp_path: Path) -> None:
    """Identical sides over all 64 cells give HOLDS."""
    rows = _study(tmp_path, 4, 10, 10)
    text = realism.render(rows, n=4)
    assert "64 of 64 cells" in text
    assert "tool_calls" in text and "equivalent" in text
    assert "HOLDS" in text and "NOT SHOWN" not in text and "FAILS" not in text


def test_the_report_says_plainly_when_a_study_cannot_decide(tmp_path: Path) -> None:
    """A noisy fixture side with the same mean gives NOT SHOWN: the study does not decide."""
    rows = _study(tmp_path, 4, 10, 10)
    for r in rows:  # same mean on every task (10.5), but a noisy fixture side: the interval widens
        if r["environment"] == "fixture":
            r["tool_calls"] = [4, 22, 9, 7][r["repeat"] - 1]
    text = realism.render(rows, n=4)
    assert "NOT SHOWN" in text and "does not decide" in text and "FAILS" not in text


def test_the_report_flags_a_difference_and_a_partial_study(tmp_path: Path) -> None:
    """A doubled side gives FAILS; 16 of 64 cells gives INCOMPLETE, never a verdict."""
    rows = _study(tmp_path, 4, 20, 10)
    text = realism.render(rows, n=4)
    assert "FAILS" in text and "differs" in text
    (tmp_path / "p").mkdir()
    partial = realism.render(_study(tmp_path / "p", 1, 10, 10), n=4)
    assert "16 of 64 cells" in partial and "INCOMPLETE" in partial


def test_a_harness_error_row_is_left_out_of_the_analysis(tmp_path: Path) -> None:
    """A harness error never counts, and the later record of a redone cell replaces the earlier."""
    rows = _study(tmp_path, 1, 10, 10)
    bad = dict(rows[0], outcome="harness_error", tool_calls=999)
    assert all(r["tool_calls"] != 999 for r in realism.usable([*rows, bad]))
    again = dict(rows[0], tool_calls=11)
    kept = [
        r
        for r in realism.usable([*rows, again])
        if r["label"] == rows[0]["label"]
        and r["environment"] == rows[0]["environment"]
        and r["repeat"] == rows[0]["repeat"]
    ]
    assert [r["tool_calls"] for r in kept] == [11], "the later record of a redone cell wins"


def test_simulated_power_falls_as_trial_noise_grows_and_replays_from_its_seed() -> None:
    """Quiet trials always show equivalence, noisy ones almost never; a 2x gap is always flagged."""
    quiet = realism.simulate_power(reps=4, cv=0.02, tau=0.0, true_ratio=1.0, sims=30, draws=200)
    noisy = realism.simulate_power(reps=4, cv=0.8, tau=0.0, true_ratio=1.0, sims=30, draws=200)
    assert quiet[0] == 1.0 and noisy[0] < 0.2
    gap = realism.simulate_power(reps=4, cv=0.02, tau=0.0, true_ratio=2.0, sims=30, draws=200)
    assert gap == (0.0, 1.0)
    again = realism.simulate_power(reps=4, cv=0.8, tau=0.0, true_ratio=1.0, sims=30, draws=200)
    assert again == noisy


class _Spy:
    """Stands in for both runners and the real-code copy; records what ran, in what order."""

    def __init__(self, out: Path, outcomes: dict[tuple[str, int, str], str] | None = None) -> None:
        """A spy with empty logs that appends cell records to `out`."""
        self.events: list[str] = []
        self.cells: list[realism.Cell] = []
        self.out = out
        self.outcomes = outcomes or {}

    def trial(self, spec: runner.TrialSpec, side: str) -> dict[str, Any]:
        """Record one cell as run on `side` and append its row."""
        cell = realism.Cell(spec.label, spec.repeat - 1, side)
        self.cells.append(cell)
        outcome = self.outcomes.get((spec.label, spec.repeat, side), "completed")
        row = _row(cell, outcome)
        with self.out.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        return row


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Spy:
    """Fakes for both runners and the real-code copy, recording what ran."""
    s = _Spy(tmp_path / "long.jsonl")
    monkeypatch.setattr(layout, "ARTIFACTS", tmp_path / "artifacts")
    monkeypatch.setattr(layout, "OVERLAYS", tmp_path / "overlays")
    monkeypatch.setattr(realism, "require_preregistered", lambda *_a, **_k: "plancommit")
    monkeypatch.setattr(runner, "load_profile", lambda _v, _p: runner.Fixture("v", tmp_path, {}))
    monkeypatch.setattr(runner, "run_trial", lambda _fx, spec, *_a, **_k: s.trial(spec, "fixture"))
    monkeypatch.setattr(
        reference, "run_reference", lambda spec, *_a, **_k: s.trial(spec, "reference")
    )
    monkeypatch.setattr(reference, "sweep", lambda _d: s.events.append("sweep"))
    monkeypatch.setattr(reference, "discard", lambda _d: s.events.append("discard"))

    def prepare(*_a: Any) -> Path:
        """Stands in for the real-code export and records that it happened."""
        s.events.append("prepare")
        return tmp_path / "tree"

    monkeypatch.setattr(reference, "prepare", prepare)
    monkeypatch.setattr(preflight, "wait_clear", lambda *_a, **_k: [])
    return s


def test_run_long_runs_every_cell_in_seed_order_and_discards_the_real_code_copy(spy: _Spy) -> None:
    """A full pass runs the cells in seed order, prepares one copy and removes it at the end."""
    assert realism.run_long(spy.out, n=1) == 0
    assert spy.cells == realism.long_cells(realism.LONG_SEED, 1)
    assert spy.events == ["sweep", "prepare", "discard"], "one copy, removed at the end"
    assert spy.out.with_suffix(".manifest.json").exists()


def test_run_long_resumes_after_a_stop_and_redoes_only_what_is_missing(spy: _Spy) -> None:
    """Cells already recorded are not run again."""
    cells = realism.long_cells(realism.LONG_SEED, 1)
    spy.out.write_text("".join(json.dumps(_row(c)) + "\n" for c in cells[:5]))
    assert realism.run_long(spy.out, n=1) == 0
    assert spy.cells == cells[5:], "the first five were already recorded"


def test_run_long_leaves_a_repeated_harness_error_as_a_gap_and_the_exit_code_says_so(
    spy: _Spy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cell that errors again stays missing (exit 3) and is retried once per pass."""
    cells = realism.long_cells(realism.LONG_SEED, 1)
    bad = cells[3]
    spy.outcomes[(bad.label, bad.rep + 1, bad.side)] = "harness_error"
    assert realism.run_long(spy.out, n=1) == 1
    assert realism.main(["--long", "--n", "1", "--out", str(spy.out)]) == 3
    assert spy.cells.count(bad) == 2, "retried once per pass, never within a pass"


def test_a_blocked_host_stops_the_study_without_forcing_and_without_leaving_real_code(
    spy: _Spy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A busy host stops the pass with exit 75, runs nothing and still removes the copy."""
    block = [preflight.Problem("gpu_busy", "busy")]
    monkeypatch.setattr(preflight, "wait_clear", lambda *_a, **_k: block)
    with pytest.raises(preflight.PreflightError):
        realism.run_long(spy.out, n=1)
    assert spy.cells == [] and spy.events[-1] == "discard"
    assert realism.main(["--long", "--n", "1", "--out", str(spy.out)]) == 75


def test_the_long_study_has_no_force_and_a_second_seed_is_refused(
    spy: _Spy, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--long --force` is an argument error; a resume with another seed is refused."""
    with pytest.raises(SystemExit):
        realism.main(["--long", "--force"])
    assert "force" in capsys.readouterr().err
    assert realism.run_long(spy.out, n=1) == 0
    with pytest.raises(ValueError, match="seed"):
        realism.run_long(spy.out, n=1, seed=99)


def test_dry_run_lists_what_would_run_and_runs_nothing(
    spy: _Spy, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--dry-run` reports the remaining cells and starts nothing."""
    assert realism.main(["--long", "--dry-run", "--n", "1", "--out", str(spy.out)]) == 0
    out = capsys.readouterr().out
    assert "16 of 16 cells remain" in out and spy.cells == [] and spy.events == []


def test_long_report_prints_the_decision_for_a_recorded_study(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--long --report` prints the cell count and the measure table of a recorded file."""
    out = tmp_path / "long.jsonl"
    rows = _study(tmp_path, 1, 10, 10)
    out.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert realism.main(["--long", "--report", "--n", "1", "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "16 of 16 cells" in printed and "tool_calls" in printed
