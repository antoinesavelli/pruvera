"""The pre-push hook refuses a history that names the disk root, in a file or a commit message.

Depends on: pytest; git and bash on PATH. `gitleaks` is stubbed: its own scan is not under test.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / ".githooks" / "pre-push"
DISK = "/srv/some-disk"
PATHS = f'{{"repo": "{DISK}/Paramo"}}'


def _git(repo: Path, *args: str) -> str:
    """Run git in `repo` and return its stdout."""
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def _commit(repo: Path, text: str, message: str = "m", name: str = "f.txt") -> str:
    """Commit `text` as the file `name` and return the commit's hash."""
    (repo / name).write_text(text)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def _push(
    repo: Path, tmp_path: Path, sha: str, *, paths: str | None = PATHS, leaks_rc: int = 0
) -> int:
    """The hook's exit code for a push of `sha`, with this paths file and a gitleaks stub that
    exits `leaks_rc` and leaves `tmp_path/ran` behind."""
    stub = tmp_path / "stub"
    stub.mkdir(exist_ok=True)
    gitleaks = stub / "gitleaks"
    gitleaks.write_text(f"#!/bin/sh\ntouch '{tmp_path / 'ran'}'\nexit {leaks_rc}\n")
    gitleaks.chmod(0o755)
    home = tmp_path / "home"
    (home / ".config" / "pruvera").mkdir(parents=True, exist_ok=True)
    config = home / ".config" / "pruvera" / "paths.json"
    config.unlink(missing_ok=True)
    if paths is not None:
        config.write_text(paths)
    env = {**os.environ, "HOME": str(home), "PATH": f"{stub}{os.pathsep}{os.environ['PATH']}"}
    stdin = f"refs/heads/main {sha} refs/heads/main {'0' * 40}\n"
    return subprocess.run(
        ["bash", str(HOOK)], cwd=repo, env=env, input=stdin, text=True, capture_output=True
    ).returncode


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """An empty git repository."""
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q")
    return path


def test_a_clean_history_passes(repo: Path, tmp_path: Path) -> None:
    """Nothing names the disk root, not even a sibling path that starts alike."""
    sha = _commit(repo, "/srv/some-other-disk/x\n")
    assert _push(repo, tmp_path, sha) == 0


def test_an_old_commit_that_named_the_root_blocks_even_after_it_was_removed(
    repo: Path, tmp_path: Path
) -> None:
    """The check reads history, not the tip: the tree at the tip is clean here."""
    _commit(repo, f"{DISK}/x\n")
    sha = _commit(repo, "clean\n")
    assert _git(repo, "show", f"{sha}:f.txt") == "clean"
    assert _push(repo, tmp_path, sha) == 1


def test_a_commit_message_that_names_the_root_blocks(repo: Path, tmp_path: Path) -> None:
    """A message is published as much as a file."""
    sha = _commit(repo, "clean\n", message=f"move to {DISK}/x")
    assert _push(repo, tmp_path, sha) == 1


def test_a_merge_that_introduces_the_root_blocks(repo: Path, tmp_path: Path) -> None:
    """An evil merge adds the text only in the merge commit, which a plain log skips."""
    first = _commit(repo, "a\n")
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    _git(repo, "checkout", "-qb", "side", first)
    _commit(repo, "b\n", name="side.txt")
    _git(repo, "checkout", "-q", branch)
    _commit(repo, "c\n", name="main.txt")
    _git(repo, "merge", "-q", "--no-commit", "--no-ff", "side")
    (repo / "evil.txt").write_text(f"{DISK}/x\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "merge")
    assert _push(repo, tmp_path, _git(repo, "rev-parse", "HEAD")) == 1


def test_no_paths_file_blocks_the_push(repo: Path, tmp_path: Path) -> None:
    """A check that cannot run does not pass."""
    assert _push(repo, tmp_path, _commit(repo, "clean\n"), paths=None) == 1


def test_the_whole_history_is_read_not_just_the_pushed_tip(repo: Path, tmp_path: Path) -> None:
    """The root was added, removed, and a later commit does not touch it: the tip diff is clean."""
    _commit(repo, f"{DISK}/x\n")
    _commit(repo, "clean\n")
    sha = _commit(repo, "more\n", name="g.txt")
    assert _push(repo, tmp_path, sha) == 1


def test_gitleaks_runs_and_its_finding_blocks_a_clean_history(repo: Path, tmp_path: Path) -> None:
    """A clean history still runs gitleaks (the stub leaves a marker); its exit 1 blocks."""
    sha = _commit(repo, "clean\n")
    assert _push(repo, tmp_path, sha) == 0 and (tmp_path / "ran").exists()
    (tmp_path / "ran").unlink()
    assert _push(repo, tmp_path, sha, leaks_rc=1) != 0 and (tmp_path / "ran").exists()


def test_the_disk_root_is_matched_literally_not_as_a_regex(repo: Path, tmp_path: Path) -> None:
    """A root with a regex character (`.`) does not block a message naming a lookalike."""
    sha = _commit(repo, "clean\n", message="move to /srv/axb/x")
    assert _push(repo, tmp_path, sha, paths='{"repo": "/srv/a.b/Paramo"}') == 0
    blocked = _commit(repo, "clean2\n", message="move to /srv/a.b/x")
    assert _push(repo, tmp_path, blocked, paths='{"repo": "/srv/a.b/Paramo"}') == 1


def test_a_sha_git_cannot_read_blocks_the_push(repo: Path, tmp_path: Path) -> None:
    """A git failure is not read as "no hit": an unknown sha stops the hook."""
    _commit(repo, "clean\n")
    assert _push(repo, tmp_path, "1" * 40) != 0


def test_an_old_commit_message_that_names_the_root_blocks_under_a_clean_tip(
    repo: Path, tmp_path: Path
) -> None:
    """The message scan walks the history too: the tip's own message and files are clean."""
    _commit(repo, "a\n", message=f"move to {DISK}/x")
    sha = _commit(repo, "b\n", message="clean")
    assert _push(repo, tmp_path, sha) == 1


def test_a_commit_whose_blob_is_gone_blocks_rather_than_reads_as_clean(
    repo: Path, tmp_path: Path
) -> None:
    """If git cannot read the history, the answer is "blocked", not "no hit"."""
    sha = _commit(repo, "clean\n")
    blob = _git(repo, "rev-parse", f"{sha}:f.txt")
    (repo / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
    assert _push(repo, tmp_path, sha) != 0


def test_deleting_a_remote_ref_passes_and_scans_nothing(repo: Path, tmp_path: Path) -> None:
    """A push that deletes a branch has the zero sha as its local side: nothing to scan."""
    _commit(repo, "clean\n")
    assert _push(repo, tmp_path, "0" * 40) == 0 and not (tmp_path / "ran").exists()


def test_a_root_commit_naming_the_root_blocks_even_with_show_root_off(
    repo: Path, tmp_path: Path
) -> None:
    """`log.showRoot=false` hides a root commit's diff, and so its text, from a plain log."""
    _git(repo, "config", "log.showRoot", "false")
    _commit(repo, f"{DISK}/x\n")
    sha = _commit(repo, "more\n", name="g.txt")
    assert _push(repo, tmp_path, sha) == 1
