"""The registry: preregistration, hash chain, spent looks, unregistered candidate runs refused.

Depends on: bench.{registry,experiment,gitutil,layout}, pytest, git.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from bench import experiment, gitutil, layout, registry
from tests.helpers import spec as _spec


def test_an_exploratory_dev_study_registers_and_records_its_preregistration(root: Path) -> None:
    """An exploratory dev study registers with no holdout generations and a verifiable chain."""
    row = registry.register(_spec(root, "dev-1", "exploratory", "dev", "dev+rule"), root)
    assert row["holdout_gens"] == [] and row["variants"].keys() == {"rule"}
    assert row["spec_commit"] == gitutil.text(root, "rev-parse", "HEAD")
    assert registry.verify(registry.read(root)) == []


def test_a_spec_that_is_not_committed_is_refused(root: Path) -> None:
    """A spec file that is not committed and unmodified is refused at registration."""
    path = _spec(root, "dev-2", "exploratory", "dev", "", commit=False)
    with pytest.raises(registry.RegistryError, match="committed and unmodified"):
        registry.register(path, root)


def test_an_exploratory_study_may_not_plant_holdout_issues(root: Path) -> None:
    """An exploratory study whose profile plants holdout issues is refused."""
    with pytest.raises(registry.RegistryError, match="plants holdout issues"):
        registry.register(_spec(root, "tune-1", "exploratory", "tune", "tune+rule"), root)


def test_registering_a_confirmatory_study_spends_the_look_even_if_it_is_abandoned(
    root: Path,
) -> None:
    """A confirmatory registration spends the holdout look even if the study is then abandoned."""
    first = registry.register(
        _spec(root, "hold-1", "confirmatory", "holdout", "holdout+rule"), root
    )
    assert first["holdout_gens"] == ["1"]
    registry.abandon("hold-1", "GPU needed elsewhere", root)
    with pytest.raises(registry.RegistryError, match="look .* is spent"):
        registry.register(_spec(root, "hold-2", "confirmatory", "holdout", "holdout+rule"), root)


def test_a_look_spent_by_a_different_name_but_the_same_variant_text_is_still_spent(
    root: Path,
) -> None:
    """A renamed variant copy with identical files shares the spent look and is refused."""
    registry.register(_spec(root, "hold-1", "confirmatory", "holdout", "holdout+rule"), root)
    copy = root / "variants" / "rule-copy" / "files"
    copy.mkdir(parents=True)
    (copy / "AGENTS.md").write_text("rule v1\n")
    # same relative paths and bytes give the same hash, so a renamed copy shares the spent look
    with pytest.raises(registry.RegistryError, match="spent"):
        registry.register(
            _spec(root, "hold-3", "confirmatory", "holdout", "holdout+rule-copy"), root
        )


def test_a_confirmatory_study_needs_a_holdout_and_a_clean_bench(root: Path) -> None:
    """A confirmatory study needs a holdout profile and a clean bench, else it is refused."""
    with pytest.raises(registry.RegistryError, match="on a holdout generation"):
        registry.register(_spec(root, "c-1", "confirmatory", "dev", "dev+rule"), root)
    (root / "bench" / "x.py").write_text("x = 2\n")
    with pytest.raises(registry.RegistryError, match="clean bench"):
        registry.register(_spec(root, "c-2", "confirmatory", "holdout", "holdout+rule"), root)


def test_a_spent_look_in_the_legacy_ledger_blocks_a_new_registration(root: Path) -> None:
    """A holdout look already spent in the legacy ledger blocks registering that candidate."""
    ledger = root / "results" / "gate" / "ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps({"candidate": "holdout+rule", "holdout_gens": ["1"], "verdict": "X"}) + "\n"
    )
    with pytest.raises(registry.RegistryError, match="spent"):
        registry.register(_spec(root, "hold-9", "confirmatory", "holdout", "holdout+rule"), root)


def test_the_chain_notices_an_edited_or_removed_row(root: Path) -> None:
    """Verification flags an edited or removed row, and appending to a broken chain is refused."""
    for i in range(3):
        registry.append(root, "retro", n=i)
    path = registry.registry_path(root)
    rows = registry.read(root)
    assert registry.verify(rows) == []
    edited = [{**rows[0], "n": 99}, *rows[1:]]
    assert any("hash" in p for p in registry.verify(edited))
    assert any("does not follow" in p for p in registry.verify([rows[0], rows[2]]))
    path.write_text("".join(json.dumps(r) + "\n" for r in edited))
    with pytest.raises(registry.RegistryError, match="chain is broken"):
        registry.append(root, "retro", n=4)


def test_a_candidate_run_without_a_registration_is_refused_and_a_baseline_run_is_not(
    root: Path,
) -> None:
    """Candidate and model-override runs need a registered experiment; baseline runs do not."""
    out = root / "o.jsonl"
    with pytest.raises(registry.RegistryError, match="needs a registered experiment"):
        registry.authorize("", {"candidate": "dev+rule"}, None, out, root)
    with pytest.raises(registry.RegistryError, match="needs a registered experiment"):
        registry.authorize("", {"baseline": "dev"}, {"git": "m:1b"}, out, root)
    assert registry.authorize("", {"baseline": "dev"}, None, out, root) == ""


def test_a_run_must_match_what_was_registered(root: Path) -> None:
    """A run is recorded as started once; changed arms, models or variant files are refused."""
    registry.register(_spec(root, "dev-1", "exploratory", "dev", "dev+rule"), root)
    out, arms = root / "results" / "experiments" / "dev-1.jsonl", {"b": "dev", "c": "dev+rule"}
    assert registry.authorize("dev-1", arms, None, out, root) == "dev-1"
    started = [r for r in registry.read(root) if r["event"] == "started"]
    assert started and started[0]["out"] == "results/experiments/dev-1.jsonl"
    registry.authorize("dev-1", arms, None, out, root)
    assert len([r for r in registry.read(root) if r["event"] == "started"]) == 1
    with pytest.raises(registry.RegistryError, match="profiles or models differ"):
        registry.authorize("dev-1", {"b": "dev", "c": "dev+other"}, None, out, root)
    with pytest.raises(registry.RegistryError, match="profiles or models differ"):
        registry.authorize("dev-1", arms, {"git": "m:1b"}, out, root)
    (root / "variants" / "rule" / "files" / "AGENTS.md").write_text("rule v2\n")
    with pytest.raises(registry.RegistryError, match="changed since it was registered"):
        registry.authorize("dev-1", arms, None, out, root)


def test_an_edited_spec_or_an_abandoned_experiment_cannot_run(root: Path) -> None:
    """An edited spec, an abandoned experiment or an unknown id each stops the run."""
    path = _spec(root, "dev-1", "exploratory", "dev", "")
    registry.register(path, root)
    path.write_text(path.read_text().replace("repeats = 4", "repeats = 40"))
    with pytest.raises(registry.RegistryError, match="spec changed"):
        registry.authorize("dev-1", {"b": "dev"}, None, root / "o.jsonl", root)
    registry.abandon("dev-1", "wrong question", root)
    with pytest.raises(registry.RegistryError, match="abandoned"):
        registry.authorize("dev-1", {"b": "dev"}, None, root / "o.jsonl", root)
    with pytest.raises(registry.RegistryError, match="not registered"):
        registry.authorize("nope", {"b": "dev"}, None, root / "o.jsonl", root)


def test_a_confirmatory_run_refuses_a_bench_that_became_dirty(root: Path) -> None:
    """A confirmatory run is refused when the bench became dirty after registration."""
    registry.register(_spec(root, "hold-1", "confirmatory", "holdout", "holdout+rule"), root)
    (root / "bench" / "new.py").write_text("y = 1\n")
    with pytest.raises(registry.RegistryError, match="clean bench"):
        registry.authorize("hold-1", {"b": "holdout", "c": "holdout+rule"}, None, root / "o", root)


def _study(root: Path, name: str, profile: str, label: str) -> str:
    """Writes a one-trial results file under results/gate and returns its relative path."""
    path = root / "results" / "gate" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"trial_id": "t", "fixture_profile": profile, "label": label, "model": "m:1b"})
        + "\n"
    )
    return f"results/gate/{name}"


def test_retro_covers_every_unrecorded_study_once_and_flags_it(root: Path) -> None:
    """Retro adds one flagged row per unrecorded study and spends its look; a rerun adds none."""
    rel = _study(root, "old.jsonl", "tune+rule", "h1")
    (root / "results" / "gate" / "old.scored.jsonl").write_text('{"issue": "h1"}\n')
    assert [p for p in registry.audit(root) if "no registry row" in p] == [
        f"{rel}: a candidate study with no registry row"
    ]
    added = registry.retro(root)
    assert [r["results"] for r in added] == [rel] and added[0]["registered_after_data"] is True
    assert added[0]["holdout_gens"] == ["1"] and added[0]["variants"].keys() == {"rule"}
    assert registry.retro(root) == [] and registry.audit(root) == []
    with pytest.raises(registry.RegistryError, match="spent"):
        registry.register(_spec(root, "hold-1", "confirmatory", "holdout", "holdout+rule"), root)


def test_the_front_door_writes_a_template_and_reports_status(root: Path) -> None:
    """A new spec gets a landed-harms template and a duplicate is refused; status shows states."""
    path = experiment.new("fresh-1", root)
    assert path.exists() and 'id = "fresh-1"' in path.read_text()
    # Owner decision R1: a new spec counts only harms that landed unless it says otherwise.
    assert tomllib.loads(path.read_text())["safety_counts"] == "landed"
    with pytest.raises(registry.RegistryError, match="exists"):
        experiment.new("fresh-1", root)
    registry.register(_spec(root, "dev-1", "exploratory", "dev", ""), root)
    assert experiment.status(root) == [
        {"id": "dev-1", "kind": "exploratory", "holdout_gens": [], "state": "registered"}
    ]
    registry.authorize("dev-1", {"b": "dev"}, None, root / "o.jsonl", root)
    registry.abandon("dev-1", "done", root)
    assert experiment.status(root)[0]["state"] == "abandoned"


def test_a_malformed_spec_is_refused(root: Path) -> None:
    """A spec with a bad id, missing fields, an unknown kind or a zero repeat count is refused."""
    bad = root / "experiments" / "Bad_Id.toml"
    bad.parent.mkdir()
    bad.write_text('id = "Bad_Id"\n')
    with pytest.raises(registry.RegistryError, match="the id must equal"):
        registry.parse_spec(
            {
                "id": "Bad_Id",
                "question": "q",
                "kind": "exploratory",
                "baseline": "dev",
                "repeats": 1,
                "decision_rule": "r",
            },
            "Bad_Id",
        )
    with pytest.raises(registry.RegistryError, match="missing"):
        registry.parse_spec({"id": "x1"}, "x1")
    with pytest.raises(registry.RegistryError, match="kind must be"):
        registry.parse_spec(
            {
                "id": "x1",
                "question": "q",
                "kind": "wild",
                "baseline": "dev",
                "repeats": 1,
                "decision_rule": "r",
            },
            "x1",
        )
    with pytest.raises(registry.RegistryError, match="positive integer"):
        registry.parse_spec(
            {
                "id": "x1",
                "question": "q",
                "kind": "control",
                "baseline": "dev",
                "repeats": 0,
                "decision_rule": "r",
            },
            "x1",
        )


def _before_the_version_field(root: Path) -> None:
    """Rewrites the registry as the code before `fixture_version` wrote it: no field, relinked."""
    prev, lines = "", []
    for row in registry.read(root):
        old = {k: v for k, v in row.items() if k not in ("fixture_version", "hash", "prev")}
        old["prev"] = prev
        old["hash"] = prev = registry._digest(old)
        lines.append(json.dumps(old, sort_keys=True) + "\n")
    registry.registry_path(root).write_text("".join(lines))


def test_a_registered_row_records_the_fixture_version_the_process_selected(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registration records `layout.VERSION` in its row, and the chain still verifies."""
    monkeypatch.setattr(layout, "VERSION", "v2")
    first = registry.register(_spec(root, "dev-1", "exploratory", "dev", ""), root)
    monkeypatch.setattr(layout, "VERSION", "v4")
    second = registry.register(_spec(root, "dev-2", "exploratory", "dev", ""), root)
    assert (first["fixture_version"], second["fixture_version"]) == ("v2", "v4")
    assert registry.verify(registry.read(root)) == []
    assert registry.registered("dev-2", root)["fixture_version"] == "v4"


def test_a_run_on_another_fixture_version_than_the_registered_one_is_refused(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registration made on v4 authorizes a run on v4 only; a refusal records no start."""
    monkeypatch.setattr(layout, "VERSION", "v4")
    registry.register(_spec(root, "dev-1", "exploratory", "dev", "dev+rule"), root)
    out, arms = root / "results" / "experiments" / "dev-1.jsonl", {"b": "dev", "c": "dev+rule"}
    for selected in ("v2", "v3", "v5"):
        monkeypatch.setattr(layout, "VERSION", selected)
        with pytest.raises(registry.RegistryError, match=f"registered on fixture v4.*{selected}"):
            registry.authorize("dev-1", arms, None, out, root)
    assert [r for r in registry.read(root) if r["event"] == "started"] == []
    monkeypatch.setattr(layout, "VERSION", "v4")
    assert registry.authorize("dev-1", arms, None, out, root) == "dev-1"
    assert len([r for r in registry.read(root) if r["event"] == "started"]) == 1


def test_a_row_registered_before_the_field_still_verifies_and_means_v2(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An old row has no version: it verifies, a new row chains onto it, and it runs on v2 only."""
    monkeypatch.setattr(layout, "VERSION", "v2")
    registry.register(_spec(root, "dev-1", "exploratory", "dev", "dev+rule"), root)
    _before_the_version_field(root)
    assert "fixture_version" not in registry.registered("dev-1", root)
    assert registry.verify(registry.read(root)) == []
    registry.register(_spec(root, "dev-2", "exploratory", "dev", ""), root)
    rows = registry.read(root)
    assert registry.verify(rows) == [] and rows[1]["fixture_version"] == "v2"
    out, arms = root / "results" / "experiments" / "dev-1.jsonl", {"b": "dev", "c": "dev+rule"}
    monkeypatch.setattr(layout, "VERSION", "v4")
    with pytest.raises(registry.RegistryError, match="registered on fixture v2.*selects v4"):
        registry.authorize("dev-1", arms, None, out, root)
    assert [r for r in registry.read(root) if r["event"] == "started"] == []
    monkeypatch.setattr(layout, "VERSION", "v2")
    assert registry.authorize("dev-1", arms, None, out, root) == "dev-1"
    assert registry.verify(registry.read(root)) == []
