"""Tests for the comparison and the reference runner, on synthetic transcripts and a fake agent."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from bench import compare, reference, runner
from bench.transcript import Transcript

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
    good = _events([("glob", {}, "completed", "")], "/work/utils/x.py:3")
    broken = _events([("glob", {}, "error", "ripgrep execution failed")], "/work/utils/x.py:3")
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
    bash = {"tool": "bash", "input": {"command": 'bash -lc "cd /work && git status --porcelain"'}}
    assert compare.command_head(bash) == "bash: cd /work"
    assert compare.command_head({"tool": "read", "input": {}}) == "read"


def test_reference_runner_uses_a_disposable_copy_and_records_the_same_shape(tmp_path: Path) -> None:
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
    cfg = tmp_path / "real.json"
    cfg.write_text(json.dumps(CONFIG))
    spec = runner.TrialSpec(
        agent="a",
        model="m",
        prompt="p",
        label="t1",
        hooks=(runner.Hook("dirty", "pkg/m.py", "# dirt\n"),),
    )
    script = 'echo \'{"type":"text","part":{"text":"hi"}}\'; echo new > added.txt'
    rec = reference.run_reference(
        spec,
        source,
        tmp_path / "artifacts",
        tmp_path / "trials",
        tmp_path / "r.jsonl",
        agent_argv=["sh", "-c", script],
        config_source=cfg,
    )
    assert (
        rec["environment"] == "reference" and rec["label"] == "t1" and rec["outcome"] == "completed"
    )
    adir = Path(rec["artifact"])
    assert "?? added.txt" in (adir / "status.txt").read_text()
    assert "# dirt" in (adir / "diff.patch").read_text()
    assert (source / "pkg" / "m.py").read_text() == "X = 1\n", "the source tree must stay untouched"
    assert not (tmp_path / "trials" / "ref-" / spec.trial_id).exists()
    assert not any((tmp_path / "trials").iterdir()), "the disposable copy must be deleted"
