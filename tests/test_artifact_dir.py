"""A trial record's recorded artifact path still resolves after the repo moves.

Depends on: bench.{layout,compare,doctor}.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bench import compare, doctor, layout


@pytest.fixture
def moved_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(layout, "ROOT", tmp_path)
    (tmp_path / "artifacts" / "abc").mkdir(parents=True)
    return tmp_path


def test_a_missing_recorded_path_is_re_rooted_under_artifacts(moved_root: Path) -> None:
    recorded = "/old/home/agent-testing/artifacts/abc"
    assert layout.artifact_dir(recorded) == moved_root / "artifacts" / "abc"


def test_an_existing_path_is_returned_unchanged(tmp_path: Path) -> None:
    assert layout.artifact_dir(tmp_path) == tmp_path


def test_a_path_without_an_artifacts_part_is_returned_unchanged() -> None:
    assert layout.artifact_dir("/nowhere/else") == Path("/nowhere/else")


def test_doctor_counts_an_old_root_record_as_present(moved_root: Path) -> None:
    old = {"artifact": "/old/home/agent-testing/artifacts/abc"}
    gone = {"artifact": "/old/home/agent-testing/artifacts/zzz"}
    assert doctor._missing_artifacts([old, gone]) == 1


def test_compare_reads_a_transcript_through_an_old_root(moved_root: Path) -> None:
    (moved_root / "artifacts" / "abc" / "transcript.jsonl").write_text("")
    compare.transcript({"artifact": "/old/home/agent-testing/artifacts/abc"})


@pytest.mark.parametrize("name", ["agent-testing", "pruvera"])
def test_normalise_strips_either_repo_name(name: str) -> None:
    text = f"/mnt/ParamoStorage/AIModels/{name}/overlays/ref-0a1b/work/x.py"
    assert compare.normalise(text) == "x.py"
