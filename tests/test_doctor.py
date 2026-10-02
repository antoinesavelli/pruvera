"""The reproducibility audit reports what is missing without writing anything."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench import doctor, layout, runner


def _write(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n")


def test_audit_names_builds_that_are_gone_and_digests_that_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        layout, "find_profile_dir", lambda h, v="v2": tmp_path if h.startswith("good") else None
    )
    monkeypatch.setattr(runner, "model_digest", lambda m, port=11434: "new-digest")
    _write(
        tmp_path / "res" / "a.jsonl",
        [
            {"fixture_tree_hash": "good" * 16, "model": "m", "model_digest": "new-digest"},
            {"fixture_tree_hash": "gone" * 16, "model": "m", "model_digest": "old-digest"},
        ],
    )
    (tmp_path / "res" / "empty.jsonl").write_text("{}\n")
    rows = doctor.audit_results(tmp_path / "res")
    assert len(rows) == 1 and rows[0]["records"] == 2
    assert rows[0]["missing_builds"] == ["gonegonegone"]
    assert rows[0]["model_digest_changed"] == ["m"]


def test_a_matching_digest_and_present_build_are_reproducible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(layout, "find_profile_dir", lambda h, v="v2": tmp_path)
    monkeypatch.setattr(runner, "model_digest", lambda m, port=11434: "")
    _write(
        tmp_path / "r.jsonl", [{"fixture_tree_hash": "x" * 64, "model": "m", "model_digest": "d"}]
    )
    row = doctor.audit_results(tmp_path)[0]
    assert row["missing_builds"] == [] and row["model_digest_changed"] == []


def test_main_exit_status_follows_missing_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(layout, "find_profile_dir", lambda h, v="v2": None)
    monkeypatch.setattr(runner, "model_digest", lambda m, port=11434: "")
    _write(tmp_path / "r.jsonl", [{"fixture_tree_hash": "x" * 64, "model": "m"}])
    assert doctor.main(["--results", str(tmp_path), "--skip-builds"]) == 1
    assert "builds gone" in capsys.readouterr().out


def test_an_unreachable_ollama_is_unchecked_not_a_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression: an empty digest (Ollama down) read as 'nothing changed'."""
    monkeypatch.setattr(layout, "find_profile_dir", lambda h, v="v2": tmp_path)
    monkeypatch.setattr(runner, "model_digest", lambda m, port=11434: "")
    _write(
        tmp_path / "r.jsonl", [{"fixture_tree_hash": "x" * 64, "model": "m", "model_digest": "d"}]
    )
    assert doctor.audit_results(tmp_path)[0]["model_digest_unchecked"] == ["m"]
    assert doctor.main(["--results", str(tmp_path), "--skip-builds"]) == 0
    out = capsys.readouterr().out
    assert "UNCHECKED" in out and "reproducible" not in out
