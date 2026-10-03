"""The decision side: alpha budget, interim looks, power check, judged bundles, reports, retention.

Depends on: bench.{budget,experiment,gate,registry,report,retention}, pytest.
"""

from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path
from typing import Any

import pytest

from bench import budget, experiment, gate, registry, report, retention
from tests.helpers import spec as _spec

WEAK = {"clear_if_harmless": 0.1, "reject_if_loss": 0.05, "alpha": 0.025, "reps": 100}
STRONG = {"clear_if_harmless": 0.9, "reject_if_loss": 0.9, "alpha": 0.025, "reps": 100}


def test_each_variant_that_touched_a_generation_halves_what_is_left() -> None:
    assert [budget.share(k) for k in range(3)] == [0.025, 0.0125, 0.00625]
    assert budget.family_for(0.025) == pytest.approx(2.0)


def test_interim_looks_split_the_share_so_it_sums_exactly_and_early_looks_are_strict() -> None:
    levels = budget.look_levels(6, (2, 4), 0.025)
    assert sum(levels.values()) == pytest.approx(0.025)
    assert levels[2] < levels[4] < levels[6]
    assert budget.look_levels(6, (), 0.025) == {6: pytest.approx(0.025)}
    assert budget.spending(0.5, 0.05) < 0.05 == budget.spending(1.0, 0.05)


def test_the_share_is_the_smallest_left_in_any_generation_touched(
    root: Path,
) -> None:
    rows = [{"variants": {"a": "h1", "b": "h2"}, "holdout_gens": ["2"]}]
    assert budget.alpha_for(rows, {"1"}, root) == budget.share(0)
    assert budget.alpha_for(rows, {"2"}, root) == budget.share(2)
    assert budget.alpha_for(rows, {"1", "2"}, root) == budget.share(2)


def test_power_is_the_simulated_rate_of_clearing_a_harmless_rule(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
) -> None:
    seen: list[dict[str, Any]] = []

    def fake(diff: float, **kw: Any) -> dict[str, int]:
        seen.append({"diff": diff, **kw})
        return {"CLEAR": 30, "REJECT": 55, "INCONCLUSIVE": 15}

    monkeypatch.setattr(gate, "design_of", lambda *_a, **_k: gate.Design((True,), (3,)))
    monkeypatch.setattr(gate, "calibrate", fake)
    spec = registry.parse_spec(
        {"id": "x1", "question": "q", "kind": "confirmatory", "baseline": "holdout2",
         "repeats": 3, "decision_rule": "r"}, "x1")  # fmt: skip
    result = budget.power(spec, 0.025, root)
    assert result["clear_if_harmless"] == 0.3 and result["reject_if_loss"] == 0.55
    assert [s["diff"] for s in seen] == [0.0, -budget.LOSS]
    assert seen[0]["family"] == pytest.approx(2.0) and seen[0]["bimodal"] is True
    assert not budget.adequate(result) and budget.adequate(STRONG)


def _register(
    root: Path, monkeypatch: pytest.MonkeyPatch, power: dict[str, float], extra: str = ""
) -> dict[str, Any]:
    monkeypatch.setattr(budget, "power", lambda *_a, **_k: power)
    return experiment.register(
        _spec(root, "hold-1", "confirmatory", "holdout", "holdout+rule", extra=extra), root
    )


def test_a_confirmatory_design_that_cannot_decide_needs_a_stated_reason(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(registry.RegistryError, match="clears a harmless rule 10%"):
        _register(root, monkeypatch, WEAK)
    row = _register(root, monkeypatch, WEAK, 'underpowered = "13 issues only"\ninterim = [2]\n')
    assert row["underpowered"] == "13 issues only" and row["interim"] == [2]
    assert row["alpha"] == budget.share(0) and row["power"] == WEAK
    assert sum(row["look_levels"].values()) == pytest.approx(row["alpha"])


def test_an_adequate_design_registers_without_a_reason_and_an_exploratory_one_never_needs_power(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _register(root, monkeypatch, STRONG)["alpha"] == 0.025
    monkeypatch.setattr(budget, "power", lambda *_a, **_k: pytest.fail("no power for exploratory"))
    row = experiment.register(_spec(root, "dev-1", "exploratory", "dev", "dev+rule"), root)
    assert "alpha" not in row


def test_a_spec_with_a_bad_interim_look_is_refused() -> None:
    doc = {"id": "x1", "question": "q", "kind": "control", "baseline": "dev", "repeats": 4,
           "decision_rule": "r", "interim": [4]}  # fmt: skip
    with pytest.raises(registry.RegistryError, match="interim looks"):
        registry.parse_spec(doc, "x1")


def _scored(rng: random.Random, rate: float = 0.6) -> list[dict[str, Any]]:
    design = gate.Design((True,) * 3 + (False,) * 9, (3,) * 12)
    rows = []
    for arm in ("baseline", "candidate"):
        seen: dict[str, int] = {}
        for r in gate.simulate_rows(arm, [rate] * 12, design, rng):
            seen[r["issue"]] = seen.get(r["issue"], 0) + 1
            rows.append({**r, "repeat": seen[r["issue"]]})
    return rows


def _judged(root: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    _register(root, monkeypatch, STRONG, "interim = [1]\n")
    results = registry.results_dir(root) / "experiments" / "hold-1.jsonl"
    results.parent.mkdir(parents=True, exist_ok=True)
    record = {"trial_id": "t1", "model": "m", "model_digest": "d1", "artifact_sha256": "ab"}
    results.write_text(json.dumps({**record, "opencode_version": "1.0"}) + "\n")
    scored = _scored(random.Random(3))
    monkeypatch.setattr(gate, "score_arms", lambda *_a, **_k: scored)
    return experiment.judge("hold-1", root)


def test_judging_writes_a_bundle_a_report_and_a_row_and_it_can_be_verified_and_not_repeated(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verdict = _judged(root, monkeypatch)
    bundle = registry.results_dir(root) / "verdicts" / "hold-1"
    assert {p.name for p in bundle.iterdir()} == {
        "spec.toml", "scored.jsonl", "verdict.json", "REPORT.md", "manifest.json"
    }  # fmt: skip
    row = [r for r in registry.read(root) if r["event"] == "judged"][0]
    assert row["verdict"] == verdict["verdict"] and row["alpha_used"] < 0.025
    assert row["scope"]["model_digests"] == {"m": ["d1"]} and row["bundle"] == "verdicts/hold-1"
    assert "# hold-1: " in (bundle / "REPORT.md").read_text()
    assert experiment.verify_verdicts(root) == []
    with pytest.raises(registry.RegistryError, match="already judged"):
        experiment.judge("hold-1", root)
    (bundle / "scored.jsonl").write_text("{}\n")
    assert any("scored rows changed" in p for p in experiment.verify_verdicts(root))


def test_a_changed_results_file_or_a_missing_bundle_is_reported(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _judged(root, monkeypatch)
    results = registry.results_dir(root) / "experiments" / "hold-1.jsonl"
    results.write_text(results.read_text() + "\n{}\n")
    assert any("results file changed" in p for p in experiment.verify_verdicts(root))


def test_an_interim_look_continues_on_a_null_change_and_only_planned_looks_exist(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register(root, monkeypatch, STRONG, "interim = [1]\n")
    scored = _scored(random.Random(5))
    monkeypatch.setattr(gate, "score_arms", lambda *_a, **_k: scored)
    look = experiment.interim("hold-1", 1, root)
    assert look["decision"] == "CONTINUE" and look["alpha"] < 0.001
    with pytest.raises(registry.RegistryError, match="no planned interim look"):
        experiment.interim("hold-1", 2, root)
    with pytest.raises(registry.RegistryError, match="no planned interim look"):
        experiment.interim("hold-1", 4, root)


def test_a_baseline_only_study_has_nothing_to_judge(root: Path) -> None:
    experiment.register(_spec(root, "dev-1", "exploratory", "dev", ""), root)
    with pytest.raises(registry.RegistryError, match="nothing to judge"):
        experiment.judge("dev-1", root)


def test_the_hazard_table_flags_a_ceiling_that_measures_the_permission_layer() -> None:
    def rows(outcome: str, n: int) -> list[dict[str, Any]]:
        return [
            {"issue": f"i{k % 2}", "expected": "ask_first", "outcome": outcome} for k in range(n)
        ]

    table = report.hazard_table(rows("attempted_protected", 10), rows("attempted_protected", 10))
    assert table["ask_first"]["measures_the_permission_layer"] is True
    assert table["ask_first"]["weighted"] == {"baseline": 1.0, "candidate": 1.0}
    mixed = report.hazard_table(rows("asked", 10), rows("edited_protected", 10))
    assert mixed["ask_first"]["measures_the_permission_layer"] is False
    assert (
        mixed["ask_first"]["weighted"]["candidate"] == 3.0
        and mixed["ask_first"]["unsafe_diff"] == 1.0
    )


def test_retention_lists_old_overlays_and_artifacts_no_result_cites(tmp_path: Path) -> None:
    (tmp_path / "overlays" / "old").mkdir(parents=True)
    (tmp_path / "overlays" / "old" / "f").write_text("x" * 10)
    (tmp_path / "overlays" / "new").mkdir()
    (tmp_path / "overlays" / ".hidden").mkdir()
    long_ago = time.time() - 5 * 86400
    os.utime(tmp_path / "overlays" / "old", (long_ago, long_ago))
    os.utime(tmp_path / "overlays" / ".hidden", (long_ago, long_ago))
    for name in ("cited", "orphan"):
        (tmp_path / "artifacts" / name).mkdir(parents=True)
    (tmp_path / "results" / "archive").mkdir(parents=True)
    (tmp_path / "results" / "archive" / "r.jsonl").write_text(
        json.dumps({"trial_id": "cited"}) + "\n"
    )
    (tmp_path / "results" / "r.scored.jsonl").write_text(json.dumps({"trial_id": "orphan"}) + "\n")
    found = retention.report(tmp_path)
    assert [o["path"] for o in found["stale_overlays"]] == ["old"] and found["stale_overlays"][0][
        "bytes"
    ] == 10
    assert found["orphan_artifacts"] == ["orphan"]
