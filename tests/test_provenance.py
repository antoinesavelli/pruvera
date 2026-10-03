"""Provenance stamps: artifact bundle hash, resumable cells, state root, scorer stamp.

Depends on: bench.{bundle,layout,runner}, bench.issues.{score,trials}.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from bench import bundle, layout, runner
from bench.issues import score, trials


def _art(tmp_path: Path) -> Path:
    art = tmp_path / "abc"
    art.mkdir()
    (art / "diff.patch").write_text("diff\n")
    (art / "transcript.jsonl").write_text("{}\n")
    return art


def test_a_bundle_digest_changes_with_any_scorer_input(tmp_path: Path) -> None:
    art = _art(tmp_path)
    first = bundle.digest(art)
    assert bundle.verify(art, first) == "" and bundle.verify(art, "") == ""
    (art / "diff.patch").write_text("diff forged\n")
    assert "changed since the trial ended" in bundle.verify(art, first)
    (art / "diff.patch").write_text("diff\n")
    (art / "stderr.txt").write_text("not a scorer input\n")
    assert bundle.verify(art, first) == ""


def test_the_digest_covers_which_file_holds_the_bytes(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    (a / "diff.patch").write_text("x")
    (b / "status.txt").write_text("x")
    assert bundle.digest(a) != bundle.digest(b)


def test_a_record_whose_bundle_changed_is_refused_by_the_scorer(tmp_path: Path) -> None:
    art = _art(tmp_path)
    record = {"artifact": str(art), "artifact_sha256": "0" * 64, "label": "x"}
    with pytest.raises(score.ScoreError, match="changed since the trial ended"):
        score.score_record(record, {}, None)  # type: ignore[arg-type]


def test_a_rerun_of_an_experiment_skips_the_cells_already_recorded(tmp_path: Path) -> None:
    out = tmp_path / "o.jsonl"
    rows = [
        {"experiment_id": "e1", "label": "i1", "arm": "baseline", "repeat": 1},
        {"experiment_id": "e1", "label": "i1", "arm": "candidate", "repeat": 1},
        {"experiment_id": "other", "label": "i2", "arm": "baseline", "repeat": 1},
        {"experiment_id": "e1", "label": "i3", "arm": "baseline"},
    ]
    out.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert trials._done_cells(out, "e1") == {
        ("i1", "baseline", 1),
        ("i1", "candidate", 1),
        ("i3", "baseline", None),
    }
    assert trials._done_cells(out, "") == set()
    assert trials._done_cells(tmp_path / "none.jsonl", "e1") == set()


def test_the_state_root_can_name_another_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_TESTING_STATE", str(tmp_path))
    try:
        fresh = importlib.reload(layout)
        assert fresh.STATE == tmp_path.resolve()
        assert fresh.RESULTS == tmp_path.resolve() / "results"
        assert fresh.FIXTURES == tmp_path.resolve() / "fixtures" / "paramo"
        assert fresh.ROOT != fresh.STATE
    finally:
        monkeypatch.delenv("AGENT_TESTING_STATE")
        importlib.reload(layout)


def test_the_harness_stamp_names_a_commit_and_a_dirty_flag() -> None:
    commit, dirty = runner.harness_state()
    assert commit and isinstance(dirty, bool)
