"""Tests for environment pinning: git-state hash in manifests, venv and data fingerprints."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bench import layout, sandbox
from bench.fixture import build, pins


@pytest.fixture
def layout_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "paramo"
    version = root / "versions" / "v9"
    tree = version / "tree"
    tree.mkdir(parents=True)
    (tree / "a.txt").write_text("a\n")
    build.git_base(tree)
    (version / "MANIFEST.json").write_text(json.dumps({"tree_hash": "x"}))
    (root / "venv" / "v9" / "bin").mkdir(parents=True)
    (root / "venv" / "v9" / "bin" / "python").write_text("#!/bin/sh\n")
    (root / "data" / layout.DATA_VERSION / "root").mkdir(parents=True)
    (root / "data" / layout.DATA_VERSION / "root" / "f.parquet").write_bytes(b"123")
    monkeypatch.setattr(layout, "FIXTURES", root)
    return root


def test_pin_writes_git_hash_and_fingerprints_and_detects_a_later_change(
    layout_root: Path,
) -> None:
    result = pins.pin("v9")
    version = layout_root / "versions" / "v9"
    manifest = json.loads((version / "MANIFEST.json").read_text())
    assert manifest["git_hash"] == sandbox.git_state_hash(version / "tree")
    assert json.loads((version / "PINS.json").read_text()) == result
    (layout_root / "data" / layout.DATA_VERSION / "root" / "f.parquet").write_bytes(b"1234")
    assert (
        sandbox.fingerprint(layout_root / "data" / layout.DATA_VERSION / "root") != result["data"]
    )


def test_git_state_hash_ignores_the_index_but_sees_config_hooks_and_refs(tmp_path: Path) -> None:
    tree = tmp_path / "t"
    tree.mkdir()
    (tree / "a.txt").write_text("a\n")
    build.git_base(tree)
    before = sandbox.git_state_hash(tree)
    (tree / ".git" / "index").write_bytes(b"different stat data")
    assert sandbox.git_state_hash(tree) == before
    (tree / ".git" / "hooks").mkdir(exist_ok=True)
    (tree / ".git" / "hooks" / "post-checkout").write_text("#!/bin/sh\ntrue\n")
    assert sandbox.git_state_hash(tree) != before
    subprocess.run(["git", "-C", str(tree), "branch", "x"], check=True, capture_output=True)
    assert sandbox.git_state_hash(tree) != before


def test_fingerprint_changes_with_a_size_a_name_or_a_mode_but_not_with_time(
    tmp_path: Path,
) -> None:
    d = tmp_path / "d"
    d.mkdir()
    (d / "f").write_text("12")
    base = sandbox.fingerprint(d)
    (d / "f").touch()
    assert sandbox.fingerprint(d) == base
    (d / "f").write_text("123")
    assert sandbox.fingerprint(d) != base
    (d / "f").write_text("12")
    (d / "f").chmod(0o755)
    assert sandbox.fingerprint(d) != base
    (d / "f").chmod(0o644)
    (d / "f").rename(d / "g")
    assert sandbox.fingerprint(d) != base


def test_layout_reads_the_source_commit_from_the_manifest_and_builds_paths(
    layout_root: Path,
) -> None:
    (layout_root / "versions" / "v9" / "MANIFEST.json").write_text(
        json.dumps({"source_commit": "abc123"})
    )
    assert layout.source_commit("v9") == "abc123"
    assert layout.tree("v9") == layout_root / "versions" / "v9" / "tree"
    assert layout.venv("v9") == layout_root / "venv" / "v9"
    assert layout.data_root() == layout_root / "data" / layout.DATA_VERSION / "root"
    assert layout.rag_dir("v9") == layout_root / "rag" / "v9"


def test_freeze_removes_write_bits_keeps_execute_and_skips_symlinks(tmp_path: Path) -> None:
    tree = tmp_path / "t"
    (tree / "d").mkdir(parents=True)
    (tree / "d" / "f.txt").write_text("x")
    (tree / "d" / "run.sh").write_text("#!/bin/sh\n")
    (tree / "d" / "run.sh").chmod(0o755)
    (tree / "link").symlink_to("d/f.txt")
    assert sandbox.freeze(tree) == 3
    try:
        assert (tree / "d" / "f.txt").stat().st_mode & 0o222 == 0
        assert (tree / "d" / "run.sh").stat().st_mode & 0o777 == 0o555
        assert (tree / "d").stat().st_mode & 0o222 == 0
        with pytest.raises(PermissionError):
            (tree / "d" / "new.txt").write_text("y")
        assert sandbox.freeze(tree) == 3, "idempotent"
    finally:
        sandbox.remove_trial_dirs(tree)
    assert not tree.exists()
