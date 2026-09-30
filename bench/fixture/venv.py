"""Build the fixture's Python environment from its pinned requirements, for use in the sandbox.

The venv is bound read-only at /venv in a trial: console-script shebangs are rewritten from the host
path to /venv, and a .pth puts the tree (/work) on sys.path the way an editable install would.
Depends on: uv and network access at build time only; nothing here runs inside a trial.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

INSIDE = "/venv"
TREE_INSIDE = "/work"


class VenvError(RuntimeError):
    """The environment could not be built."""


def rewrite_shebangs(venv: Path, inside: str = INSIDE) -> list[str]:
    """Point every bin/ script that names this venv's python at `inside`; return files changed."""
    changed: list[str] = []
    host = str(venv.resolve())
    for script in sorted((venv / "bin").iterdir()):
        if script.is_symlink() or not script.is_file():
            continue
        head = script.read_bytes()[:2]
        if head != b"#!":
            continue
        text = script.read_text(errors="surrogateescape")
        first, _, rest = text.partition("\n")
        if host in first:
            script.write_text(first.replace(host, inside) + "\n" + rest, errors="surrogateescape")
            changed.append(script.name)
    return changed


def add_tree_pth(venv: Path, tree_inside: str = TREE_INSIDE) -> Path:
    """Write a .pth so `import config`, `import scripts` resolve to the tree mounted at /work."""
    site = next((venv / "lib").glob("python*/site-packages"))
    pth = site / "fixture_tree.pth"
    pth.write_text(tree_inside + "\n")
    return pth


def build(tree: Path, dest: Path, python: str = "/usr/bin/python3.12") -> None:
    """Create `dest` from the tree's requirements.txt (+ requirements-nodeps.txt), pip seeded."""
    uv = shutil.which("uv")
    if uv is None:
        raise VenvError("uv not found")
    if dest.exists():
        raise VenvError(f"already exists: {dest}")
    steps = [
        [uv, "venv", str(dest), "--python", python, "--seed", "-q"],
        [
            uv,
            "pip",
            "install",
            "-q",
            "--python",
            str(dest / "bin" / "python"),
            "-r",
            "requirements.txt",
        ],
    ]
    nodeps = tree / "requirements-nodeps.txt"
    if nodeps.exists():
        steps.append(
            [
                uv,
                "pip",
                "install",
                "-q",
                "--python",
                str(dest / "bin" / "python"),
                "--no-deps",
                "-r",
                "requirements-nodeps.txt",
            ]
        )
    for cmd in steps:
        done = subprocess.run(cmd, cwd=tree, capture_output=True, text=True, check=False)
        if done.returncode != 0:
            raise VenvError(f"{' '.join(cmd[:3])} failed: {done.stderr[-400:]}")
    rewrite_shebangs(dest)
    add_tree_pth(dest)
