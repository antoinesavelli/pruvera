"""bench.readback: what a trial left in its repo is read through a hardened, failing-loudly git."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from bench import readback, sandbox


def _spec(tmp_path: Path) -> sandbox.Spec:
    return sandbox.Spec(
        base=tmp_path / "b", upper=tmp_path / "u", work=tmp_path / "w", xdg=tmp_path / "x"
    )


def _fake_run(monkeypatch: pytest.MonkeyPatch, stdout: str = "", rc: int = 0) -> list[str]:
    scripts: list[str] = []

    def run(_spec: object, cmd: list[str], timeout: float | None = None) -> Any:
        scripts.append(cmd[-1])
        return subprocess.CompletedProcess(cmd, rc, stdout, "boom")

    monkeypatch.setattr(sandbox, "run", run)
    return scripts


def test_the_read_back_script_ignores_user_git_files_and_reads_through_a_fresh_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripts = _fake_run(monkeypatch)
    with pytest.raises(readback.ReadBackError):
        readback.read_back(_spec(tmp_path), "abc123")  # no marker in the empty output
    script = scripts[0]
    assert "XDG_CONFIG_HOME=/nonexistent" in script and "GIT_CONFIG_GLOBAL=/dev/null" in script
    assert "core.attributesFile=/dev/null" in script and "--text" in script
    assert "GIT_INDEX_FILE=/tmp/readback.index" in script and "read-tree abc123" in script
    assert script.startswith("set -e;"), "any git failure stops the read-back"


def test_a_nonzero_exit_or_a_timeout_is_a_failed_read_back_not_an_empty_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_run(monkeypatch, rc=128)
    with pytest.raises(readback.ReadBackError, match="exited 128"):
        readback.read_back(_spec(tmp_path), "abc")

    def slow(*_a: object, **_k: object) -> None:
        raise subprocess.TimeoutExpired("git", 1)

    monkeypatch.setattr(sandbox, "run", slow)
    with pytest.raises(readback.ReadBackError, match="timed out"):
        readback.read_back(_spec(tmp_path), "abc")
    with pytest.raises(readback.ReadBackError, match="timed out"):
        readback.git_state(_spec(tmp_path), "abc")


def test_read_back_splits_status_from_the_diff_at_its_random_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, str] = {}

    def run(_spec: object, cmd: list[str], timeout: float | None = None) -> Any:
        marker = next(w for w in cmd[-1].split("'") if w.startswith("---DIFF-"))
        captured["marker"] = marker
        out = f" M a.py\n?? ---DIFF-fake---\n{marker}\ndiff --git a/a.py b/a.py\n"
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(sandbox, "run", run)
    status, diff = readback.read_back(_spec(tmp_path), "abc")
    assert status == " M a.py\n?? ---DIFF-fake---\n" and diff == "diff --git a/a.py b/a.py\n"


def test_git_state_parses_commits_staged_files_and_stashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(_spec: object, cmd: list[str], timeout: float | None = None) -> Any:
        found = re.search(r"@@[0-9a-f]{32}@@", cmd[-1])
        assert found
        sep = found.group(0)
        out = (
            f"{sep}aaa\tdocs: note\nREADME.md\n\n{sep}bbb\tmerge\nside.txt\nmain.txt\n"
            f"{sep}STAGED\npeer.md\n{sep}STASH\nstash@{{0}}: WIP\n"
        )
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(sandbox, "run", run)
    state = readback.git_state(_spec(tmp_path), "base")
    assert state["commits"] == [
        {"sha": "aaa", "subject": "docs: note", "files": ["README.md"]},
        {"sha": "bbb", "subject": "merge", "files": ["side.txt", "main.txt"]},
    ]
    assert state["staged"] == ["peer.md"] and state["stashes"] == ["stash@{0}: WIP"]


def test_a_bound_venv_reads_back_as_one_ignored_line_and_a_hidden_file_still_shows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    (repo / ".venv" / "lib").mkdir(parents=True)
    for n in range(30):
        (repo / ".venv" / "lib" / f"f{n}.py").write_text("x\n")
    (repo / ".gitignore").write_text(".venv/\nsecret.txt\n")
    (repo / "kept.py").write_text("a = 1\n")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"}  # fmt: skip
    for args in (["init", "-q"], ["add", ".gitignore", "kept.py"], ["commit", "-qm", "base"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, env=env, capture_output=True)
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    (repo / "secret.txt").write_text("hidden by an ignore rule\n")
    (repo / "new.py").write_text("b = 2\n")

    def run(_spec: object, cmd: list[str], timeout: float | None = None) -> Any:
        script = cmd[-1].replace("/tmp/readback", f"{tmp_path}/readback")
        script = script.replace(sandbox.WORKDIR, str(repo))
        return subprocess.run(["sh", "-c", script], capture_output=True, text=True, env=env)

    monkeypatch.setattr(sandbox, "run", run)
    status, _ = readback.read_back(_spec(tmp_path), base)
    lines = sorted(status.splitlines())
    assert lines == ["!! .venv/", "!! secret.txt", "?? new.py"]
