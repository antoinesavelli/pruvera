"""Tests for the comparison and the reference runner, on synthetic transcripts and a fake agent."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from bench import compare, reference, runner, sandbox
from bench.transcript import Transcript
from tests.test_sandbox import _bwrap_works

CONFIG = {"model": "ollama/m", "provider": {"ollama": {}}, "agent": {"a": {"model": "ollama/m"}}}


def _events(tools: list[tuple[str, dict[str, Any], str, str]], text: str) -> str:
    lines = []
    for tool, inp, status, err in tools:
        state = {"status": status, "input": inp, "error": err, "output": err}
        lines.append(json.dumps({"type": "tool_use", "part": {"tool": tool, "state": state}}))
    lines.append(json.dumps({"type": "text", "part": {"text": text}}))
    lines.append(json.dumps({"type": "step_finish", "part": {"tokens": {"total": 5}}}))
    return "\n".join(lines) + "\n"


def _record(tmp_path: Path, env: str, i: int, events: str) -> dict[str, Any]:
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
    bash = {
        "tool": "bash",
        "input": {"command": f'bash -lc "cd {sandbox.WORKDIR} && git status --porcelain"'},
    }
    assert compare.command_head(bash) == f"bash: cd {sandbox.WORKDIR}"
    assert compare.command_head({"tool": "read", "input": {}}) == "read"


def _git_source(tmp_path: Path) -> Path:
    source = tmp_path / "src"
    (source / "pkg").mkdir(parents=True)
    (source / "pkg" / "m.py").write_text("X = 1\n")
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(source),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "base"]):
        subprocess.run(["git", "-C", str(source), *args], env=env, check=True, capture_output=True)
    return source


EVENT = '{"type":"text","part":{"text":"hi"}}'
needs_bwrap = pytest.mark.skipif(not _bwrap_works(), reason="unprivileged bwrap unavailable")


def _reference(tmp_path: Path, source: Path, script: str, spec: runner.TrialSpec) -> dict[str, Any]:
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
    source = _git_source(tmp_path)
    hooks = (runner.Hook("dirty", "pkg/m.py", "# dirt\n"),)
    spec = runner.TrialSpec(agent="a", model="m", prompt="p", label="t1", hooks=hooks, net="none")
    script = f"echo '{EVENT}'; echo new > {sandbox.WORKDIR}/added.txt"
    rec = _reference(tmp_path, source, script, spec)
    assert rec["environment"] == "reference" and rec["label"] == "t1"
    assert rec["trial_id"] == f"ref-{spec.trial_id}" and rec["outcome"] == "completed"
    adir = Path(rec["artifact"])
    assert "?? added.txt" in (adir / "status.txt").read_text()
    assert "# dirt" in (adir / "diff.patch").read_text()
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
    source = _git_source(tmp_path)
    spec = runner.TrialSpec(agent="a", model="m", prompt="p", net="none")

    def blocked(force: bool = False) -> list[Any]:
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
    assert not (tmp_path / "trials").exists() or not any((tmp_path / "trials").iterdir())


def test_discard_removes_a_stale_prepared_copy_including_unreadable_dirs(tmp_path: Path) -> None:
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
    return {
        "environment": side,
        "label": label,
        "tool_calls": calls,
        "secs": secs,
        "outcome": "completed" if ok else "timeout",
        "tool_errors": err,
    }


def test_effects_call_equal_sides_equivalent_and_a_2x_gap_different() -> None:
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
