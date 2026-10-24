"""The one-off spikes still import and their pure helpers still compute what they claim."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str) -> ModuleType:
    """Loads a spike script from the spikes directory as a module and returns it."""
    spec = importlib.util.spec_from_file_location(f"spike_{name}", ROOT / "spikes" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_latency_trace_prints_a_summary_per_side(capsys: pytest.CaptureFixture[str]) -> None:
    """The latency trace prints a per-side summary with first-byte times and the later median."""
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
    """The retrieval spike ranks files by best chunk and scores recall, MRR, BM25 and z-scores."""
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
    """The startup spike imports, its prompt names the status file, and `run_once` is callable."""
    spike = _load("latency_startup")
    assert "STATUS.md" in spike.PROMPT and callable(spike.run_once)


def _bakeoff_row(issue: str, model: str, success: bool, **extra: object) -> dict[str, object]:
    """Builds one scored bakeoff trial row for an issue and model, with optional overrides."""
    row: dict[str, object] = {"trial_id": f"{model}-{issue}-{extra.get('repeat', 1)}"}
    row |= {"issue": issue, "model": model, "success": success, "kind": "logic_bug_caught_by_test"}
    row |= {"expected": "fix", "outcome": "fixed" if success else "missed", "answer_kind": "text"}
    return row | {"secs": 10.0, "tool_calls": 3} | extra


def _write_study(directory: Path, name: str, rows: list[dict[str, object]]) -> None:
    """Writes a study's raw and scored JSONL files into a directory from the given rows."""
    import json

    raw = [{"trial_id": r["trial_id"], "outcome": "completed", "steps": 4, "fixture_version": "v2"}
           | {"seeded": False, "experiment_id": ""} for r in rows]  # fmt: skip
    (directory / f"{name}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in raw))
    (directory / f"{name}.scored.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_the_public_summary_drops_held_out_rows_and_prints_no_issue_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The public summary drops held-out issue rows from its counts and prints no issue id."""
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
    """The public summary exits with a refusal when no holdout profile is registered."""
    spike = _load("public_summary")
    monkeypatch.setattr(spike.registry, "holdout_issues", lambda root: {})
    monkeypatch.setattr(sys, "argv", ["x", str(tmp_path), "--out", str(tmp_path / "S.md")])
    with pytest.raises(SystemExit, match="refusing to run"):
        spike.main()


def test_the_public_summary_leaves_out_a_harness_error_but_keeps_an_ordinary_failure() -> None:
    """A `harness_error` row is an infrastructure fault whatever steps the agent took."""
    spike = _load("public_summary")
    lost = ({"issue": "q-1", "outcome": "harness_error"}, {"outcome": "harness_error", "steps": 5})
    assert spike.drop_reason(*lost, frozenset()) == "infrastructure fault"
    missed = ({"issue": "q-1", "outcome": "missed"}, {"outcome": "completed", "steps": 5})
    assert spike.drop_reason(*missed, frozenset()) == ""


def test_every_interval_the_readme_quotes_is_in_the_public_summary() -> None:
    """Every k/n interval the README quotes appears in the public summary text."""
    import re

    summary = ROOT / "results" / "public" / "SUMMARY.md"
    readme = " ".join((ROOT / "README.md").read_text().split())  # a rate may wrap across lines
    quoted = re.findall(r"\d+/\d+ \[\d\.\d\d, \d\.\d\d\]", readme)
    assert quoted, "the README quotes its rates in the summary's k/n [lo, hi] form"
    missing = sorted(set(q for q in quoted if q not in summary.read_text()))
    assert missing == [], f"README rates not printed by spikes/public_summary.py: {missing}"


def test_the_cost_report_passes_its_own_checks(capsys: pytest.CaptureFixture[str]) -> None:
    """The cost report's self-test returns 0 and prints over ten lines, none starting with FAIL."""
    spike = _load("cost_report")
    assert spike.self_test() == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) > 10 and not [line for line in lines if line.startswith("FAIL")]


def _jsonl(path: Path, rows: list[dict[str, object]]) -> Path:
    """Writes the rows as JSON lines to path and returns it."""
    import json

    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def test_the_grader_audit_passes_its_own_selftest(capsys: pytest.CaptureFixture[str]) -> None:
    """The grader audit's self-test (tables, draw, claims, rule, validation) runs to the end."""
    _load("grader_audit").selftest()
    assert "selftest ok" in capsys.readouterr().out


def test_the_owner_spot_check_refuses_cannot_tell_but_the_blind_rater_may_use_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An owner spot label becomes the truth, so `ingest --spot` refuses CANNOT_TELL and stores
    nothing; the blind rater's CANNOT_TELL is still accepted, and a definite owner label stored."""
    import json

    spike = _load("grader_audit")
    item = {"family": "flag", "group": "flag", "outcome": "flagged", "scorer_label": "REPORTS"}
    sample = tmp_path / "sample.json"
    sample.write_text(json.dumps({"items": {"r001": item | {"wave": 1}}}))
    spot = tmp_path / "spot.json"
    spot.write_text(json.dumps({"rater": "claude-blind-1", "items": ["r001"]}))
    ratings = tmp_path / "ratings"
    monkeypatch.setattr(spike, "SAMPLE", sample)
    monkeypatch.setattr(spike, "SPOT", spot)
    monkeypatch.setattr(spike, "RATINGS", ratings)
    line = {"item": "r001", "label": "CANNOT_TELL", "confidence": 2, "note": "unclear"}
    unsure = _jsonl(tmp_path / "unsure.jsonl", [line])
    with pytest.raises(SystemExit, match="must be definite, not CANNOT_TELL"):
        spike.cmd_ingest(unsure, "owner-spot", True)
    assert not (ratings / "owner-spot.jsonl").exists()
    spike.cmd_ingest(unsure, "claude-blind-1", False)
    sure = _jsonl(tmp_path / "sure.jsonl", [line | {"label": "MISSES"}])
    spike.cmd_ingest(sure, "owner-spot", True)
    assert json.loads((ratings / "owner-spot.jsonl").read_text())["label"] == "MISSES"
    capsys.readouterr()


def test_the_audit_report_labels_its_pooled_false_pass_rates_unweighted() -> None:
    """A family's false-pass k/n pools scorer outcomes sampled at unequal rates: the JSON names
    the pooled outcomes and the interval's weighting, and every markdown false-pass line says so."""
    spike = _load("grader_audit")
    rows: list[dict[str, object]] = [
        {"issue": "a", "outcome": "scoped", "scorer_label": "SCOPED", "truth": "SCOPED"},
        {"issue": "b", "outcome": "scoped", "scorer_label": "SCOPED", "truth": "SCOPED"},
        {"issue": "b", "outcome": "no_commit", "scorer_label": "NO_COMMIT", "truth": "NO_COMMIT"},
        {"issue": "c", "outcome": "swept", "scorer_label": "SWEPT", "truth": "SWEPT"},
    ]
    for row in rows:
        row |= {"rater_label": row["truth"], "cause": "", "pending": False}
    report = spike.family_report("commit_scope", rows)
    safe = report["claims"]["safe"]
    assert safe["positives_by_outcome"] == {"scoped": 2, "no_commit": 1}
    assert safe["false_pass_interval"].startswith("Wilson, unweighted")
    lines = spike.render_report([report], rows, "").splitlines()
    claims = [line for line in lines if line.startswith("- ") and "false pass" in line]
    header = [line for line in lines if line.startswith("| family |")]
    assert len(claims) == 2 and all("unweighted" in line for line in claims + header)
    assert any(line.startswith("False-pass rates and intervals are unweighted") for line in lines)


def test_the_kinds_report_leaves_faults_out_of_n_and_scores_a_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A harness error and a 0-step agent_error (the registered fault) are left out of k/n and
    counted as invalid; a timed-out run keeps its score, as the screen's decision rule has it."""
    spike = _load("kinds_report")
    ends = [("t1", "completed", 4), ("t2", "timeout", 9), ("t3", "harness_error", 0)]
    ends.append(("t4", "agent_error", 0))
    raw = [
        {"trial_id": t, "outcome": end, "steps": steps, "tool_calls": 1} for t, end, steps in ends
    ]
    _jsonl(tmp_path / "kinds-dev-m.jsonl", raw)
    scored = [
        {"trial_id": t, "outcome": o, "success": t == "t1", "kind": "logic_bug_caught_by_test"}
        for t, o in (("t1", "fixed"), ("t2", "missed"), ("t3", "harness_error"), ("t4", "missed"))
    ]
    _jsonl(tmp_path / "kinds-dev-m.scored.jsonl", scored)
    monkeypatch.setattr(spike, "BAKEOFF", tmp_path)
    monkeypatch.setattr(sys, "argv", ["kinds_report.py"])
    assert spike.main() == 0
    row = capsys.readouterr().out.splitlines()[1].split()
    assert row[0] == "m" and row[1] == "1/2" and row[-1] == "2"


def test_reliability_keeps_a_timeout_as_the_agents_failure_and_drops_only_faults(
    tmp_path: Path,
) -> None:
    """`load_runs` drops unscorable rows, harness errors and an agent_error with no tool call (the
    0-step fault) and counts them; a timeout and an agent_error that acted stay as failures."""
    spike = _load("reliability")
    ends = [
        ("completed", "fixed", 3),
        ("timeout", "missed", 3),
        ("harness_error", "harness_error", 0),
        ("agent_error", "missed", 0),
        ("agent_error", "missed", 2),
        ("completed", "unscorable", 3),
    ]
    rows = [
        {"model": "m", "issue": "x", "repeat": n, "trial_outcome": end, "outcome": outcome}
        | {"success": outcome == "fixed", "tool_calls": calls}
        for n, (end, outcome, calls) in enumerate(ends, start=1)
    ]
    runs = spike.load_runs(_jsonl(tmp_path / "m.scored.jsonl", rows))
    assert runs.wins == {"x": [True, False, False]} and runs.dropped == 3


def test_the_reliability_spike_passes_its_own_self_test(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The reliability self-test (published ICC example, kappa cases, ties) returns 0."""
    assert _load("reliability").self_test() == 0
    assert "FAIL" not in capsys.readouterr().out
