"""One way to run git against a repo: `git -C <repo> ...`, an explicit environment, bytes or text.

Depends on: nothing else in bench.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def run(
    repo: Path,
    *args: str,
    env: dict[str, str] | None = None,
    stdin: str = "",
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    """`git -C repo args`; `env=None` inherits the caller's environment."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        env=env,
        input=stdin.encode(),
        check=check,
        capture_output=True,
    )


def raw(repo: Path, *args: str, env: dict[str, str] | None = None) -> bytes:
    """Standard output of a git command that must succeed, as bytes."""
    return run(repo, *args, env=env).stdout


def text(repo: Path, *args: str, env: dict[str, str] | None = None, stdin: str = "") -> str:
    """Standard output of a git command that must succeed, decoded and stripped."""
    return run(repo, *args, env=env, stdin=stdin).stdout.decode(errors="replace").strip()
