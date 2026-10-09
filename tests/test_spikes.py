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


def test_the_startup_spike_still_imports_and_names_its_prompt() -> None:
    spike = _load("latency_startup")
    assert "STATUS.md" in spike.PROMPT and callable(spike.run_once)


def _bakeoff_row(issue: str, model: str, success: bool, **extra: object) -> dict[str, object]:
    row: dict[str, object] = {"trial_id": f"{model}-{issue}-{extra.get('repeat', 1)}"}
    row |= {"issue": issue, "model": model, "success": success, "kind": "logic_bug_caught_by_test"}
    row |= {"expected": "fix", "outcome": "fixed" if success else "missed", "answer_kind": "text"}
    return row | {"secs": 10.0, "tool_calls": 3} | extra


def _write_study(directory: Path, name: str, rows: list[dict[str, object]]) -> None:
    import json

    raw = [{"trial_id": r["trial_id"], "outcome": "completed", "steps": 4, "fixture_version": "v2"}
           | {"seeded": False, "experiment_id": ""} for r in rows]  # fmt: skip
    (directory / f"{name}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in raw))
    (directory / f"{name}.scored.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_the_public_summary_drops_held_out_rows_and_prints_no_issue_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    spike = _load("public_summary")
    monkeypatch.setattr(spike.registry, "holdout_issues", lambda root: {"1": frozenset({"h-999"})})
    monkeypatch.setattr(spike.registry, "read", lambda root: [])
    stage1 = [_bakeoff_row("q-111", "m1", True), _bakeoff_row("q-222", "m1", False)]
    stage1.append(_bakeoff_row("h-999", "m1", True))
    _write_study(tmp_path, "kinds-dev-m1", stage1)
    stage2 = [_bakeoff_row(i, m, m == "m1" or r == 1, repeat=r)
              for m in ("m1", "m2") for i in ("q-111", "q-222") for r in (1, 2, 3)]  # fmt: skip
    _write_study(tmp_path, "stage2-m1", stage2)
    for name in ("handoff-m1", "handoff3-m1"):
        commit = dict(kind="shared_tree_hazard", expected="commit_scope")
        rows = [_bakeoff_row("q-333", "m1", True, outcome="scoped", **commit)]
        rows.append(_bakeoff_row("h-999", "m1", False, outcome="swept", **commit))
        _write_study(tmp_path, name, rows)
    out = tmp_path / "SUMMARY.md"
    monkeypatch.setattr(sys, "argv", ["x", str(tmp_path), "--out", str(out), "--root", "/nowhere"])
    assert spike.main() == 0
    text = out.read_text()
    assert not any(i in text for i in ("q-111", "q-222", "q-333", "h-999"))
    assert "| stage1 | 1 | 2 | 1 held out |" in text and "| handoff | 1 | 1 | 1 held out |" in text
    assert "| m1 | 1/2 [0.09, 0.91] | 1/2 [0.09, 0.91] | - |" in text, "held-out success dropped"
    assert "| m1 | 1/2 [0.09, 0.91] | 6/6 [0.61, 1.00] | yes | 2 | 0 |" in text
    assert "| m2 | - | 2/6 [0.10, 0.70] | no | 0 | 0 |" in text
    assert "unsafe 0/1 [0.00, 0.79]" in text, "the held-out sweep is dropped, not hidden in n"
    capsys.readouterr()


def test_the_public_summary_refuses_without_a_holdout_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spike = _load("public_summary")
    monkeypatch.setattr(spike.registry, "holdout_issues", lambda root: {})
    monkeypatch.setattr(sys, "argv", ["x", str(tmp_path), "--out", str(tmp_path / "S.md")])
    with pytest.raises(SystemExit, match="refusing to run"):
        spike.main()


def test_every_interval_the_readme_quotes_is_in_the_public_summary() -> None:
    import re

    summary = ROOT / "results" / "public" / "SUMMARY.md"
    readme = " ".join((ROOT / "README.md").read_text().split())  # a rate may wrap across lines
    quoted = re.findall(r"\d+/\d+ \[\d\.\d\d, \d\.\d\d\]", readme)
    assert quoted, "the README quotes its rates in the summary's k/n [lo, hi] form"
    missing = sorted(set(q for q in quoted if q not in summary.read_text()))
    assert missing == [], f"README rates not printed by spikes/public_summary.py: {missing}"
