"""The opt-in `seed` of an experiment spec: parsing, preregistration, authorization, the runs.

A spec's `seed` is a base seed: repeat r of every issue and arm runs with `seed + r - 1`, so the
repeats of a seeded study still differ (a fixed seed would make them near-copies of one sample),
the arms are paired by repeat, and the study replays. A spec without `seed` is unseeded.

Depends on: bench.{registry,experiment,agentconfig,runner,preflight}, bench.issues.trials, pytest.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import pytest

from bench import agentconfig, experiment, preflight, registry, runner
from bench.issues import trials
from tests.helpers import needs_catalogue
from tests.helpers import spec as _spec

BASE = {
    "id": "s-1",
    "question": "q",
    "kind": "exploratory",
    "baseline": "dev",
    "repeats": 4,
    "decision_rule": "r",
}


def parse(**extra: Any) -> registry.Spec:
    return registry.parse_spec({**BASE, **extra}, "s-1")


def test_a_spec_is_unseeded_unless_it_sets_a_seed() -> None:
    assert parse().seed is None
    assert parse(seed=4242).seed == 4242
    assert parse(seed=0).seed == 0, "seed 0 is a seed, not a missing one"


@pytest.mark.parametrize("bad", [-1, True, "7", 1.5, 2**31])
def test_a_seed_that_is_not_one_is_refused_at_parse_time(bad: Any) -> None:
    with pytest.raises(registry.RegistryError, match="seed"):
        parse(seed=bad)


def test_every_repeat_of_a_seeded_study_must_get_a_valid_seed() -> None:
    top = agentconfig.MAX_SEED
    assert parse(seed=top - 3).seed == top - 3  # repeats = 4: the last repeat gets exactly `top`
    with pytest.raises(registry.RegistryError, match="seed"):
        parse(seed=top - 2)
    # the safety issues run more repeats than the rest, and they need seeds too
    with pytest.raises(registry.RegistryError, match="seed"):
        parse(seed=top - 5, safety_repeats=12)
    assert parse(seed=top - 11, safety_repeats=12).safety_repeats == 12


def test_the_seed_of_a_repeat_is_the_base_plus_the_repeat_less_one() -> None:
    assert registry.trial_seed(None, 3) is None
    assert [registry.trial_seed(4242, r) for r in (1, 2, 3)] == [4242, 4243, 4244]
    assert registry.trial_seed(0, 1) == 0


def test_a_registration_records_its_seed_or_that_it_has_none(root: Path) -> None:
    seeded = registry.register(
        _spec(root, "seeded-1", "exploratory", "dev", "", extra="seed = 4242\n"), root
    )
    plain = registry.register(_spec(root, "plain-1", "exploratory", "dev", ""), root)
    assert seeded["seed"] == 4242 and plain["seed"] is None
    assert registry.registered("seeded-1", root)["seed"] == 4242
    assert registry.verify(registry.read(root)) == []


def _authorize(root: Path, name: str, seed: int | None) -> str:
    out = root / "results" / "experiments" / f"{name}.jsonl"
    return registry.authorize(name, {"b": "dev"}, None, out, root, seed=seed)


def test_a_run_must_use_the_registered_seed(root: Path) -> None:
    registry.register(_spec(root, "s-1", "exploratory", "dev", "", extra="seed = 4242\n"), root)
    assert _authorize(root, "s-1", 4242) == "s-1"
    for wrong in (7, None):
        with pytest.raises(registry.RegistryError, match="seed differs"):
            _authorize(root, "s-1", wrong)


def test_a_study_registered_without_a_seed_cannot_be_run_seeded(root: Path) -> None:
    registry.register(_spec(root, "p-1", "exploratory", "dev", ""), root)
    assert _authorize(root, "p-1", None) == "p-1"
    with pytest.raises(registry.RegistryError, match="seed differs"):
        _authorize(root, "p-1", 4242)


def test_a_study_registered_before_seeds_existed_still_runs_unseeded(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry.register(_spec(root, "old-1", "exploratory", "dev", ""), root)
    rows = registry.read(root)
    # a row written before the field existed has no `seed` key at all
    monkeypatch.setattr(
        registry,
        "read",
        lambda _root=root: [{k: v for k, v in r.items() if k != "seed"} for r in rows],
    )
    assert _authorize(root, "old-1", None) == "old-1"


def test_a_seeded_run_without_a_registration_is_refused(root: Path) -> None:
    out = root / "o.jsonl"
    with pytest.raises(registry.RegistryError, match="seeded run needs a registered experiment"):
        registry.authorize("", {"baseline": "dev"}, None, out, root, seed=4242)
    assert registry.authorize("", {"baseline": "dev"}, None, out, root, seed=None) == ""


def test_the_spec_template_offers_a_seed_but_does_not_set_one(root: Path) -> None:
    text = experiment.new("fresh-1", root).read_text()
    assert "# seed = " in text and "seed" not in tomllib.loads(text)


def test_a_verdict_says_when_its_study_was_seeded() -> None:
    row = {"kind": "confirmatory", "seed": 4242}
    noted = experiment._caveats(row, {}, [])
    assert any("seeded study (base seed 4242" in n and "real session" in n for n in noted)
    assert not any("seeded" in n for n in experiment._caveats({**row, "seed": None}, {}, []))
    assert not any("seeded" in n for n in experiment._caveats({"kind": "confirmatory"}, {}, []))


IDS = ("mut-helpers-60", "hand-injection-comment")  # catalogue issues, as in test_orchestration


def _fixture(name: str) -> runner.Fixture:
    manifest = {"tree_hash": "", "fixture_base_commit": ""}
    return runner.Fixture(name, Path("/nonexistent"), manifest, issue_ids=IDS)


@pytest.mark.parametrize(("extra", "expected"), [("seed = 4242\n", 4242), ("", None)])
def test_run_hands_the_registered_seed_to_the_campaign(
    root: Path, monkeypatch: pytest.MonkeyPatch, extra: str, expected: int | None
) -> None:
    registry.register(_spec(root, "e-1", "exploratory", "dev", "", extra=extra), root)
    seen: dict[str, Any] = {}
    monkeypatch.setattr(runner, "load_profile", lambda _v, p: _fixture(p))
    monkeypatch.setattr(trials, "run_arms", lambda arms, n, out, **kw: seen.update(kw) or out)
    experiment.run("e-1", root=root)
    assert seen["seed"] == expected


class Recorder:
    """Stands in for runner.run_trial: remembers each cell and the seed it was asked to use."""

    def __init__(self) -> None:
        self.cells: list[tuple[str, str, int, int | None]] = []

    def __call__(
        self, _fx: Any, spec: runner.TrialSpec, _a: Path, _t: Path, out: Path, **_k: Any
    ) -> dict[str, Any]:
        self.cells.append((spec.label, spec.arm, spec.repeat, spec.seed))
        row = {"label": spec.label, "arm": spec.arm, "repeat": spec.repeat, "outcome": "completed"}
        with out.open("a") as fh:
            fh.write(json.dumps({**row, "experiment_id": spec.experiment_id}) + "\n")
        return row


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> tuple[Recorder, list[dict[str, Any]]]:
    rec, asked = Recorder(), []

    def authorize(experiment_id: str, *_a: Any, **kw: Any) -> str:
        asked.append({"experiment": experiment_id, **kw})
        return experiment_id

    monkeypatch.setattr(runner, "run_trial", rec)
    monkeypatch.setattr(preflight, "wait_clear", lambda *_a, **_k: [])
    monkeypatch.setattr(trials.registry, "authorize", authorize)
    return rec, asked


@needs_catalogue
def test_each_repeat_gets_its_own_seed_and_the_arms_share_it(
    recorder: tuple[Recorder, list[dict[str, Any]]], tmp_path: Path
) -> None:
    rec, asked = recorder
    arms = {"baseline": _fixture("b"), "candidate": _fixture("c")}
    trials.run_arms(arms, 3, tmp_path / "o.jsonl", experiment="e-1", seed=100)
    assert asked[0]["seed"] == 100, "authorize is told the base seed it must match"
    assert {(c[2], c[3]) for c in rec.cells} == {(1, 100), (2, 101), (3, 102)}
    by_repeat: dict[int, set[tuple[str, int | None]]] = {}
    for _label, arm, repeat, seed in rec.cells:
        by_repeat.setdefault(repeat, set()).add((arm, seed))
    assert all(len(pairs) == 2 for pairs in by_repeat.values())  # both arms, one seed per repeat


@needs_catalogue
def test_an_unseeded_campaign_sends_no_seed(
    recorder: tuple[Recorder, list[dict[str, Any]]], tmp_path: Path
) -> None:
    rec, asked = recorder
    trials.run_arms({"a": _fixture("a")}, 2, tmp_path / "o.jsonl", experiment="e-1")
    assert asked[0]["seed"] is None and {c[3] for c in rec.cells} == {None}


@needs_catalogue
def test_a_resumed_campaign_gives_the_remaining_repeat_the_seed_it_would_have_had(
    recorder: tuple[Recorder, list[dict[str, Any]]], tmp_path: Path
) -> None:
    rec, _ = recorder
    out = tmp_path / "o.jsonl"
    trials.run_arms({"a": _fixture("a")}, 2, out, experiment="e-1", seed=100)
    first = len(rec.cells)
    trials.run_arms({"a": _fixture("a")}, 3, out, experiment="e-1", seed=100)
    assert {(c[2], c[3]) for c in rec.cells[first:]} == {(3, 102)}
