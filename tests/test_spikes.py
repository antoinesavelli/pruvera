"""The one-off spikes still import and their pure helpers still compute what they claim."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"spike_{name}", ROOT / "spikes" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_latency_trace_prints_a_summary_per_side(capsys: pytest.CaptureFixture[str]) -> None:
    spike = _load("latency_trace")
    calls = [
        {"first_byte_s": 3.2, "system": "a", "tools": "b", "body_len": 10},
        {"first_byte_s": 0.3, "system": "a", "tools": "b", "body_len": 20},
    ]
    spike._print_side("fixture", [{"secs": 13.0, "phases": {}, "calls": calls}])
    out = capsys.readouterr().out
    assert (
        "fixture: 1 trials" in out and "[3.2]" in out and "later-call first-byte median 0.3" in out
    )


def test_retrieval_spike_ranks_files_and_scores_recall_and_mrr() -> None:
    np = pytest.importorskip("numpy")
    pytest.importorskip("yaml")
    spike = _load("retrieval_spike")
    chunks = [("a.md", "alpha beta"), ("b.md", "beta gamma"), ("a.md", "alpha again")]
    ranked = spike.rank_files(chunks, np.array([0.1, 0.9, 0.5]))
    assert ranked == ["b.md", "a.md"], "files are ranked once, by their best chunk"
    assert spike.summarise([1, 3, None, 12]) == {
        "recall@1": 0.25,
        "recall@3": 0.5,
        "recall@5": 0.5,
        "recall@10": 0.5,
        "mrr": round((1 + 1 / 3 + 1 / 12) / 4, 3),
    }
    scores = spike.bm25(chunks, "gamma")
    assert scores.argmax() == 1 and scores[0] == 0
    assert abs(float(spike.z(np.array([1.0, 2.0, 3.0])).mean())) < 1e-6
