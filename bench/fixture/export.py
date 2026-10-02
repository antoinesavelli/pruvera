"""Export one pinned commit to a plain directory without touching the source repo's index or tree.

Depends on: bench.gitutil, git, plus git-crypt when the source repo is encrypted and unlocked
(plaintext out).
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from bench import gitutil

CIPHERTEXT_HEADER = b"\x00GITCRYPT\x00"


class ExportError(RuntimeError):
    """The export failed or produced something unusable."""


def _git(repo: Path, args: list[str], env: dict[str, str]) -> bytes:
    try:
        return gitutil.run(repo, *args, env=env).stdout
    except subprocess.CalledProcessError as exc:
        raise ExportError(
            f"git {' '.join(args[:2])} failed: {exc.stderr.decode(errors='replace')[:300]}"
        ) from exc


def resolve(repo: Path, rev: str) -> str:
    """Full commit hash for `rev`."""
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
    return _git(repo, ["rev-parse", "--verify", f"{rev}^{{commit}}"], env).decode().strip()


def export_commit(repo: Path, commit: str, dest: Path) -> list[str]:
    """Check `commit` out into `dest` via a private index (filters run); return its paths."""
    if dest.exists() and any(dest.iterdir()):
        raise ExportError(f"destination not empty: {dest}")
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fixture-index-") as tmp:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "GIT_INDEX_FILE": str(Path(tmp) / "index"),
        }
        _git(repo, ["read-tree", commit], env)
        _git(repo, ["checkout-index", "-a", "-f", f"--prefix={dest}/"], env)
        listing = _git(repo, ["ls-files", "-z"], env).decode("utf-8", "surrogateescape")
    return sorted(p for p in listing.split("\0") if p)


def ciphertext_files(root: Path) -> list[str]:
    """Files that still start with the git-crypt header: the fixture would silently test garbage."""
    bad: list[str] = []
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as fh:
                if fh.read(len(CIPHERTEXT_HEADER)) == CIPHERTEXT_HEADER:
                    bad.append(path.relative_to(root).as_posix())
    return sorted(bad)
