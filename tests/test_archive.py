"""Archiving moves results with their derived files, records a reason, and never overwrites."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench import archive, doctor, layout, runner


def _results(tmp_path: Path) -> Path:
    res = tmp_path / "results"
    (res / "gate").mkdir(parents=True)
    for name in ("a.jsonl", "a.scored.jsonl", "a.rederived-2026-10-02.json", "b.jsonl"):
        (res / "gate" / name).write_text(
            json.dumps({"fixture_tree_hash": "x" * 64, "model": "m"}) + "\n"
        )
    return res


def test_files_move_with_their_companions_and_leave_a_reason(tmp_path: Path) -> None:
    res = _results(tmp_path)
    moved = archive.archive([res / "gate" / "a.jsonl"], "build gone", res, day="2026-10-02")
    assert sorted(p.name for p in moved) == [
        "a.jsonl",
        "a.rederived-2026-10-02.json",
        "a.scored.jsonl",
    ]
    assert [p.name for p in (res / "gate").iterdir()] == ["b.jsonl"]
    assert all(p.parent == res / "archive" / "2026-10-02" / "gate" for p in moved)
    index = [
        json.loads(line) for line in (res / "archive" / "INDEX.jsonl").read_text().splitlines()
    ]
    assert {r["file"] for r in index} == {
        "gate/a.jsonl",
        "gate/a.scored.jsonl",
        "gate/a.rederived-2026-10-02.json",
    }
    assert {r["reason"] for r in index} == {"build gone"}


def test_a_file_named_with_its_companion_moves_once_and_a_cited_file_stays(tmp_path: Path) -> None:
    res = _results(tmp_path)
    both = [res / "gate" / "a.jsonl", res / "gate" / "a.scored.jsonl"]
    assert len(archive.archive(both, "r", res, day="2026-10-02")) == 3
    cited = {"candidate": "c", "results": "x/b.jsonl"}
    (res / "gate" / "ledger.jsonl").write_text(json.dumps(cited) + "\n")
    with pytest.raises(ValueError, match="cited by a ledger row"):
        archive.archive([res / "gate" / "b.jsonl"], "r", res)
    assert (res / "gate" / "b.jsonl").exists()


def test_nothing_is_overwritten_and_nothing_moves_twice(tmp_path: Path) -> None:
    res = _results(tmp_path)
    clash = res / "archive" / "2026-10-02" / "gate"
    clash.mkdir(parents=True)
    (clash / "b.jsonl").write_text("keep\n")
    with pytest.raises(FileExistsError):
        archive.archive([res / "gate" / "b.jsonl"], "r", res, day="2026-10-02")
    assert (res / "gate" / "b.jsonl").exists() and (clash / "b.jsonl").read_text() == "keep\n"
    assert not (res / "archive" / "INDEX.jsonl").exists(), "a refused move leaves no index line"
    with pytest.raises(ValueError, match="already archived"):
        archive.archive([clash / "b.jsonl"], "r", res)


def test_the_doctor_audits_the_active_set_and_lists_the_archive_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(layout, "find_profile_dir", lambda h, v="v2": None)
    monkeypatch.setattr(runner, "model_digest", lambda m, port=11434: "")
    res = _results(tmp_path)
    (res / "gate" / "b.jsonl").write_text(json.dumps({"fixture_tree_hash": "x" * 64, "model": "m"}))
    (res / "gate" / "a.jsonl").write_text(json.dumps({"fixture_tree_hash": "x" * 64, "model": "m"}))
    assert doctor.main(["--results", str(res), "--skip-builds"]) == 1
    archive.archive([res / "gate" / "a.jsonl", res / "gate" / "b.jsonl"], "gone", res)
    capsys.readouterr()
    assert doctor.main(["--results", str(res), "--skip-builds", "--strict"]) == 0
    assert "archive (not audited): 4 files" in capsys.readouterr().out


def test_the_command_moves_the_named_files(tmp_path: Path) -> None:
    res = _results(tmp_path)
    argv = ["--results", str(res), "--reason", "r", str(res / "gate" / "b.jsonl")]
    assert archive.main(argv) == 0
    assert not (res / "gate" / "b.jsonl").exists()
