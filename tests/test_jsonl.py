"""bench.jsonl: results files are read the same way everywhere."""

from __future__ import annotations

from pathlib import Path

import pytest

from bench.jsonl import read_jsonl


def test_records_are_read_skipping_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "r.jsonl"
    path.write_text('{"a": 1}\n\n  \n{"a": 2}\n')
    assert read_jsonl(path) == [{"a": 1}, {"a": 2}]


def test_a_bad_line_raises_unless_skipping_is_asked_for(tmp_path: Path) -> None:
    path = tmp_path / "r.jsonl"
    path.write_text('{"a": 1}\nnot json\n[1, 2]\n{"a": 3}\n')
    with pytest.raises(ValueError):
        read_jsonl(path)
    assert read_jsonl(path, skip_bad=True) == [{"a": 1}, {"a": 3}]
    path.write_text('{"a": 1}\n[1, 2]\n')
    with pytest.raises(ValueError, match="not a JSON object"):
        read_jsonl(path)
