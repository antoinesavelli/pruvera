"""Tests for environment pinning: git-state hash in manifests, venv and data fingerprints."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bench import cli, sandbox
from bench.fixture import build, pins


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "paramo"
    version = root / "versions" / "v9"
    tree = version / "tree"
    tree.mkdir(parents=True)
    (tree / "a.txt").write_text("a\n")
    build.git_base(tree)
    (version / "MANIFEST.json").write_text(json.dumps({"tree_hash": "x"}))
    (root / "venv" / "v9" / "bin").mkdir(parents=True)
    (root / "venv" / "v9" / "bin" / "python").write_text("#!/bin/sh\n")
    (root / "data" / cli.DATA_VERSION / "root").mkdir(parents=True)
    (root / "data" / cli.DATA_VERSION / "root" / "f.parquet").write_bytes(b"123")
    monkeypatch.setattr(cli, "FIXTURES", root)
    return root


def test_pin_writes_git_hash_and_fingerprints_and_detects_a_later_change(layout: Path) -> None:
    result = pins.pin("v9")
    version = layout / "versions" / "v9"
    manifest = json.loads((version / "MANIFEST.json").read_text())
    assert manifest["git_hash"] == sandbox.git_state_hash(version / "tree")
    assert json.loads((version / "PINS.json").read_text()) == result
    (layout / "data" / cli.DATA_VERSION / "root" / "f.parquet").write_bytes(b"1234")
    assert sandbox.fingerprint(layout / "data" / cli.DATA_VERSION / "root") != result["data"]


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
