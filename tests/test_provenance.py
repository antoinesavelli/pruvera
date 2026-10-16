"""Provenance stamps: artifact bundle hash, resumable cells, state root, scorer stamp.

Depends on: bench.{bundle,layout,runner}, bench.issues.{score,trials}.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType

import pytest

from bench import bundle, layout, runner
from bench.issues import score, trials


def _art(tmp_path: Path) -> Path:
    """Makes an artifact folder holding a diff patch and a transcript, and returns its path."""
    art = tmp_path / "abc"
    art.mkdir()
    (art / "diff.patch").write_text("diff\n")
    (art / "transcript.jsonl").write_text("{}\n")
    return art


def test_a_bundle_digest_changes_with_any_scorer_input(tmp_path: Path) -> None:
    """A bundle digest changes when a scorer input changes, not when a non-input file is added."""
    art = _art(tmp_path)
    first = bundle.digest(art)
    assert bundle.verify(art, first) == "" and bundle.verify(art, "") == ""
    (art / "diff.patch").write_text("diff forged\n")
    assert "changed since the trial ended" in bundle.verify(art, first)
    (art / "diff.patch").write_text("diff\n")
    (art / "stderr.txt").write_text("not a scorer input\n")
    assert bundle.verify(art, first) == ""


def test_the_digest_covers_which_file_holds_the_bytes(tmp_path: Path) -> None:
    """The same bytes under diff.patch and under status.txt give different bundle digests."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    (a / "diff.patch").write_text("x")
    (b / "status.txt").write_text("x")
    assert bundle.digest(a) != bundle.digest(b)


def test_a_record_whose_bundle_changed_is_refused_by_the_scorer(tmp_path: Path) -> None:
    """A record whose stored bundle digest no longer matches is refused by the scorer as changed."""
    art = _art(tmp_path)
    record = {"artifact": str(art), "artifact_sha256": "0" * 64, "label": "x"}
    with pytest.raises(score.ScoreError, match="changed since the trial ended"):
        score.score_record(record, {}, None)  # type: ignore[arg-type]


def test_a_rerun_of_an_experiment_skips_the_cells_already_recorded(tmp_path: Path) -> None:
    """Cells already recorded for the same experiment are done; a blank id or no file gives none."""
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
    """The state-root environment variable points results and fixtures paths at another checkout."""
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


VERSION_ENV = "AGENT_TESTING_FIXTURE_VERSION"


@pytest.fixture
def reload_layout(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str | None], ModuleType]]:
    """Reloads `layout` under a fixture-version variable (unset for None); the original returns."""

    def reload(value: str | None) -> ModuleType:
        """Sets or unsets the variable, re-imports `layout`, and returns it."""
        if value is None:
            monkeypatch.delenv(VERSION_ENV, raising=False)
        else:
            monkeypatch.setenv(VERSION_ENV, value)
        return importlib.reload(layout)

    yield reload
    monkeypatch.undo()  # the caller's own value of the variable, before layout is read again
    importlib.reload(layout)


def test_the_fixture_version_is_v2_unless_the_environment_names_another_build(
    reload_layout: Callable[[str | None], ModuleType],
) -> None:
    """Unset or empty is v2; `v4` selects that build, and the path defaults follow at import."""
    assert reload_layout(None).VERSION == "v2"
    assert reload_layout("").VERSION == "v2"
    fresh = reload_layout("v4")
    assert fresh.VERSION == "v4"
    assert fresh.version_dir() == fresh.FIXTURES / "versions" / "v4"
    assert fresh.tree() == fresh.FIXTURES / "versions" / "v4" / "tree"
    assert fresh.venv() == fresh.FIXTURES / "venv" / "v4"
    assert fresh.rag_dir() == fresh.FIXTURES / "rag" / "v4"
    assert fresh.tree("v2") == fresh.FIXTURES / "versions" / "v2" / "tree"
    assert fresh.DATA_VERSION == "v3"  # the data slice is versioned on its own
    assert reload_layout("v10").VERSION == "v10"


@pytest.mark.parametrize(
    "bad",
    ["4", "v", "V4", "v4a", "v-1", "v2.0", "../v2", "v4/../v2", " v4", "v4\n", "v\u0664", "latest"],
)
def test_a_fixture_version_that_is_not_v_and_digits_is_refused_at_import(
    reload_layout: Callable[[str | None], ModuleType], bad: str
) -> None:
    """A value that could leave the versions directory, or is no version name, stops the import."""
    with pytest.raises(ValueError, match=VERSION_ENV):
        reload_layout(bad)


def test_the_harness_stamp_names_a_commit_and_a_dirty_flag() -> None:
    """The harness state is a non-empty commit id and a boolean dirty flag."""
    commit, dirty = runner.harness_state()
    assert commit and isinstance(dirty, bool)
