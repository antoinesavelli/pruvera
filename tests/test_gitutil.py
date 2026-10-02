"""The shared git helper runs against the named repo, with the given environment and input."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bench import gitutil

ENV = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": "/dev/null", "HOME": "/nonexistent"}


def test_text_raw_and_run_agree_and_stdin_and_env_are_honoured(tmp_path: Path) -> None:
    gitutil.text(tmp_path, "init", "-q", "-b", "main", env=ENV)
    assert gitutil.text(tmp_path, "symbolic-ref", "--short", "HEAD", env=ENV) == "main"
    (tmp_path / "a.txt").write_text("x\n")
    (tmp_path / "b.txt").write_text("y\n")
    gitutil.text(
        tmp_path, "add", "--pathspec-from-file=-", "--pathspec-file-nul", env=ENV, stdin="a.txt\0"
    )
    assert gitutil.raw(tmp_path, "ls-files", "-z", env=ENV) == b"a.txt\0"
    assert gitutil.run(tmp_path, "ls-files", env=ENV).stdout == b"a.txt\n"


def test_a_failing_command_raises_unless_the_caller_checks_for_itself(tmp_path: Path) -> None:
    with pytest.raises(subprocess.CalledProcessError) as caught:
        gitutil.text(tmp_path, "rev-parse", "HEAD", env=ENV)
    assert b"not a git repository" in caught.value.stderr
    done = gitutil.run(tmp_path, "rev-parse", "HEAD", env=ENV, check=False)
    assert done.returncode != 0 and done.stdout == b""
