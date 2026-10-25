"""The seed-replay analysis computes exactly the measures the study registered, and no others.

Depends on: spikes/seed_replay.py, bench.stats, pytest.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from bench import stats

ROOT = Path(__file__).resolve().parents[1]
Event = dict[str, Any]


@pytest.fixture
def spike(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Loads the seed-replay spike module with its experiments and artifacts dirs in tmp_path."""
    path = ROOT / "spikes" / "seed_replay.py"
    spec = importlib.util.spec_from_file_location("spike_seed_replay", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "EXPERIMENTS", tmp_path / "experiments")
    monkeypatch.setattr(module, "ARTIFACTS", tmp_path / "artifacts")
    return module


def call(tool: str, **args: object) -> Event:
    """Builds a tool_use event for the named tool whose input is the keyword arguments."""
    return {"type": "tool_use", "part": {"tool": tool, "state": {"input": args}}}


def say(text: str) -> Event:
    """Builds a text event carrying the given text."""
    return {"type": "text", "part": {"text": text}}


def stepped(*events: Event) -> list[Event]:
    """The events as opencode streams them, each action its own step."""
    return [e for event in events for e in ({"type": "step_start"}, event, {"type": "step_finish"})]


def tool(name: str) -> tuple[str, str]:
    """Returns the (tool, name) action pair for a tool call."""
    return ("tool", name)


def text(body: str) -> tuple[str, str]:
    """Returns the (text, body) action pair for a text part."""
    return ("text", body)


def cell(spike: ModuleType, acts: list[Any], outcome: str = "fixed", missing: bool = False) -> Any:
    """A Cell whose actions are each one step."""
    return spike.Cell([[a] for a in acts], outcome, missing)


def test_actions_keep_event_order_ignore_other_events_and_sort_arguments(
    spike: ModuleType, tmp_path: Path
) -> None:
    """Proves `actions` keeps event order, skips non-action events and sorts argument keys."""

    def write(events: list[Event]) -> Path:
        """Writes the events as JSON lines to a new numbered file in tmp_path and returns it."""
        path = tmp_path / f"t{len(list(tmp_path.glob('t*.jsonl')))}.jsonl"
        path.write_text("".join(json.dumps(e) + "\n" for e in events))
        return path

    first = write(
        [{"type": "step_start"}, call("grep", b=2, a=1), say("hi"), {"type": "step_finish"}]
    )
    assert spike.actions(first) == [("tool", 'grep {"a": 1, "b": 2}'), ("text", "hi")]
    assert spike.actions(write([call("grep", a=1, b=2), say("hi")])) == spike.actions(first)


def test_steps_group_actions_by_model_turn_and_drop_turns_with_no_action(
    spike: ModuleType, tmp_path: Path
) -> None:
    """Proves `steps` splits at each step_start and leaves out a step with no tool call or text."""
    events = [
        {"type": "step_start"},
        say("look"),
        call("ls"),
        {"type": "step_finish"},
        {"type": "step_start"},  # a reasoning-only turn
        {"type": "step_finish"},
        {"type": "step_start"},
        say("done"),
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    assert spike.steps(path) == [[("text", "look"), ("tool", "ls {}")], [("text", "done")]]


@pytest.mark.parametrize(
    ("a", "b", "first", "same_trajectory", "lead"),
    [
        ([tool("ls"), tool("cat"), text("x")], [tool("ls"), tool("cat"), text("x")], True, True, 3),
        ([tool("ls"), tool("cat"), text("x")], [tool("ls"), tool("rm"), text("x")], True, False, 1),
        ([tool("ls"), text("x")], [tool("cat"), text("x")], False, False, 0),
        ([tool("ls"), text("x")], [tool("ls"), text("y")], True, False, 1),
        # only the final text counts toward trajectory replay, not what the agent said on the way
        (
            [text("one"), tool("ls"), text("x")],
            [text("two"), tool("ls"), text("x")],
            False,
            True,
            0,
        ),
        # one run acted and the other did not: that is a disagreement
        ([tool("ls")], [], False, False, 0),
        # two empty transcripts show nothing to agree on: not compared at all
        ([], [], None, None, None),
    ],
)
def test_the_registered_measures_for_one_pair(
    spike: ModuleType,
    a: list[Any],
    b: list[Any],
    first: bool | None,
    same_trajectory: bool | None,
    lead: int | None,
) -> None:
    """Proves `measure` gives the first-action, trajectory, outcome and leading values per pair."""
    got = spike.measure(cell(spike, a), cell(spike, b))
    assert got == {
        "first_action": first,
        "trajectory": same_trajectory,
        "outcome": True,
        "leading_steps": lead,
        "leading_actions": lead,
        "missing_answer": False,
    }


def test_leading_steps_count_whole_model_turns(spike: ModuleType) -> None:
    """Proves a turn that differs in any action ends the leading steps, not only its actions."""
    a = spike.Cell([[tool("ls"), tool("cat")], [text("x")]], "fixed")
    b = spike.Cell([[tool("ls"), tool("rm")], [text("x")]], "fixed")
    got = spike.measure(a, b)
    assert got["leading_steps"] == 0 and got["leading_actions"] == 1


def test_a_different_scored_outcome_is_outcome_disagreement_alone(spike: ModuleType) -> None:
    """Proves equal actions with different scored outcomes fail only the outcome measure."""
    got = spike.measure(cell(spike, [tool("ls")], "fixed"), cell(spike, [tool("ls")], "missed"))
    assert got["trajectory"] and got["first_action"] and not got["outcome"]


def test_an_unscorable_cell_is_not_compared_on_outcome(spike: ModuleType) -> None:
    """Proves two unscorable cells, or one, give no outcome agreement, only a pair left out."""
    lost = cell(spike, [tool("ls")], spike.UNSCORABLE)
    assert spike.measure(lost, lost)["outcome"] is None
    assert spike.measure(lost, cell(spike, [tool("ls")], "fixed"))["outcome"] is None
    assert spike.measure(lost, lost)["trajectory"] is True, "the actions still compare"


def test_pairs_with_nothing_to_compare_leave_n_and_are_counted_apart(spike: ModuleType) -> None:
    """Proves `summarise` drops not-comparable pairs from a measure's n and reports how many."""
    empty, lost = cell(spike, []), cell(spike, [tool("ls")], spike.UNSCORABLE)
    good = cell(spike, [tool("ls")])
    pairs = [
        ("i1", spike.measure(good, good)),
        ("i2", spike.measure(empty, empty)),
        ("i3", spike.measure(lost, lost)),
    ]
    got = spike.summarise(pairs)
    assert (got["first_action"]["k"], got["first_action"]["n"]) == (2, 2)
    assert (got["trajectory"]["k"], got["trajectory"]["n"]) == (2, 2)
    assert got["trajectory"]["not_comparable"] == 1
    assert (got["outcome"]["k"], got["outcome"]["n"], got["outcome"]["not_comparable"]) == (2, 2, 1)
    assert got["median_leading_steps"] == 1, "the empty pair gives no leading count"


def write_run(
    root: Path,
    name: str,
    trials: list[tuple[str, int, list[Event]]],
    scored: list[Event] | None = None,
) -> None:
    """One run: (issue, repeat, events) per trial, scored `fixed` with a text answer unless
    `scored` gives each trial's scored-row fields."""
    results, rows = [], []
    for n, (issue, repeat, events) in enumerate(trials):
        tid = f"{name}-{n}"
        (root / "artifacts" / tid).mkdir(parents=True)
        (root / "artifacts" / tid / "transcript.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in events)
        )
        results.append(
            {"trial_id": tid, "label": issue, "repeat": repeat, "outcome": "completed", "steps": 2}
        )
        fields = scored[n] if scored else {"outcome": "fixed", "success": True}
        rows.append({"trial_id": tid, "answer_kind": "text", **fields})
    (root / "experiments").mkdir(exist_ok=True)
    for suffix, out in ((".jsonl", results), (".scored.jsonl", rows)):
        (root / "experiments" / f"{name}{suffix}").write_text(
            "".join(json.dumps(r) + "\n" for r in out)
        )


def test_load_run_takes_the_last_usable_trial_of_a_cell_and_skips_the_rest(
    spike: ModuleType, tmp_path: Path
) -> None:
    """Proves `load_run` keeps a cell's usable scored trial and skips errored or unscored ones."""
    write_run(tmp_path, "r", [("i1", 1, [call("ls")])])
    rows = [
        {"trial_id": "bad", "label": "i1", "repeat": 1, "outcome": "harness_error", "steps": 0},
        {"trial_id": "r-0", "label": "i1", "repeat": 1, "outcome": "completed", "steps": 2},
        {"trial_id": "dead", "label": "i2", "repeat": 1, "outcome": "agent_error", "steps": 0},
        {"trial_id": "unscored", "label": "i3", "repeat": 1, "outcome": "completed", "steps": 2},
    ]
    (tmp_path / "experiments" / "r.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    run = spike.load_run("r")
    assert list(run) == [("i1", 1)] and run[("i1", 1)].actions == [("tool", "ls {}")]


def test_a_failure_without_an_answer_is_its_own_outcome_and_counted_apart(
    spike: ModuleType, tmp_path: Path
) -> None:
    """Proves a missing answer the scorer fails is `no_answer`, so it never agrees with a wrong
    answer; a success with no final text keeps its outcome; both are counted as missing answers."""
    events = [call("ls")]
    write_run(
        tmp_path,
        "a",
        [("i1", 1, events), ("i2", 1, events), ("i3", 1, events)],
        [
            {"outcome": "missed", "success": False, "answer_kind": "empty"},
            {"outcome": "scoped", "success": True, "answer_kind": "empty"},
            {"outcome": "missed", "success": False, "answer_kind": "tool_json"},
        ],
    )
    write_run(
        tmp_path,
        "b",
        [("i1", 1, events), ("i2", 1, events), ("i3", 1, events)],
        [
            {"outcome": "missed", "success": False},  # a wrong answer, given as text
            {"outcome": "scoped", "success": True},
            {"outcome": "missed", "success": False, "answer_kind": "empty"},
        ],
    )
    a, b = spike.load_run("a"), spike.load_run("b")
    assert [a[(i, 1)].outcome for i in ("i1", "i2", "i3")] == ["no_answer", "scoped", "no_answer"]
    assert spike.run_counts(a)["missing_answers"] == 3
    assert spike.run_counts(b)["missing_answers"] == 1
    got = spike.summarise(spike.seeded_pairs(a, b))
    assert (got["outcome"]["k"], got["outcome"]["n"]) == (2, 3), "only i1 disagrees"
    assert got["with_missing_answer"]["pairs"] == 3
    assert got["with_missing_answer"]["outcome"] == {"k": 2, "n": 3}


def test_reference_pairs_are_the_repeat_pairs_of_each_issue_and_seeded_pairs_the_shared_cells(
    spike: ModuleType,
) -> None:
    """Proves reference pairs are each issue's repeat pairs and seeded pairs the shared cells."""
    same = cell(spike, [tool("ls")], "x")
    run = {(i, r): same for i in ("a", "b") for r in (1, 2, 3)}
    assert [issue for issue, _ in spike.reference_pairs(run)] == ["a"] * 3 + ["b"] * 3
    assert len(spike.reference_pairs({k: v for k, v in run.items() if k[1] < 3})) == 2
    fewer = {k: v for k, v in run.items() if k != ("a", 2)}
    assert [issue for issue, _ in spike.seeded_pairs(run, fewer)] == ["a", "a", "b", "b", "b"]


def clustered(agreeing: int, issues: int, per: int = 3) -> list[tuple[str, dict[str, Any]]]:
    """Pairs whose issues agree all together or differ all together, the first `agreeing` agree."""
    return [
        (
            f"i{i}",
            {
                "first_action": i < agreeing,
                "trajectory": i < agreeing,
                "outcome": True,
                "leading_steps": 1,
                "leading_actions": 1,
                "missing_answer": False,
            },
        )
        for i in range(issues)
        for _ in range(per)
    ]


def test_the_design_effect_is_one_for_spread_pairs_and_the_issue_size_for_clustered_ones(
    spike: ModuleType,
) -> None:
    """Proves the effective n is n when agreement is spread evenly over issues, the number of
    issues when an issue's pairs agree together, and the number of issues when none agree."""
    spread = {f"i{i}": [True, False, False] for i in range(30)}
    assert spike.design_effect(spread) == 1.0
    together = spike.rate(clustered(7, 10), "trajectory")
    assert together["design_effect"] == pytest.approx(3.0) and together["n_effective"] == 10
    none = spike.rate(clustered(0, 31), "trajectory")
    assert none["n_effective"] == pytest.approx(31.0)
    assert none["wilson_effective"][1] == pytest.approx(stats.wilson(0, 31)[1])


def test_pairs_that_share_trials_are_not_judged_as_independent(spike: ModuleType) -> None:
    """Proves the claim is judged on the effective-n intervals: issue-clustered agreement whose
    plain Wilson intervals separate, but whose effective ones overlap, makes no claim."""
    seeded, reference = spike.summarise(clustered(7, 10)), spike.summarise(clustered(3, 10))
    for m in ("first_action", "trajectory"):
        assert seeded[m]["wilson"][0] > reference[m]["wilson"][1], "plain intervals separate"
        assert seeded[m]["wilson_effective"][0] < reference[m]["wilson_effective"][1]
    got = spike.decide(seeded, reference)
    assert got["a_seed_replays_a_campaign"] is False
    assert got["failing"] == ["first_action", "trajectory"]
    assert got["with_plain_wilson"]["a_seed_replays_a_campaign"] is True
    assert got["plain_wilson_agrees"] is False


def part(k: int, n: int) -> dict[str, Any]:
    """Builds a summary dict with k, n and the Wilson interval for k successes in n."""
    return {"k": k, "n": n, "wilson": list(stats.wilson(k, n))}


def test_the_claim_needs_separated_intervals_and_a_gap_of_a_fifth(spike: ModuleType) -> None:
    """Proves `exceeds` needs non-overlapping intervals and a gap of at least 0.2."""
    assert spike.exceeds(part(90, 93), part(10, 93), "wilson")
    assert not spike.exceeds(part(45, 93), part(40, 93), "wilson"), "the intervals overlap"
    # separated intervals (large n) but a gap of only 0.1, and a gap under 0.2 is not claimed
    seeded, reference = part(300, 500), part(250, 500)
    assert seeded["wilson"][0] > reference["wilson"][1]
    assert not spike.exceeds(seeded, reference, "wilson")
    assert not spike.exceeds(part(0, 0), part(0, 93), "wilson"), "nothing compared, no claim"


def build_study(root: Path, replays: bool) -> None:
    """Eight issues, three repeats. Seeded run b matches a (or drifts after the first call);
    unseeded repeats differ from the first call on."""
    issues = [f"i{n}" for n in range(8)]

    def seeded(name: str) -> list[tuple[str, int, list[Event]]]:
        """Lists a seeded run's trials; run b alters its second call unless replaying."""
        drift = 0 if replays or name == "a" else 1
        return [
            (i, r, stepped(call("ls", d=i), call("cat", n=r + drift), say(f"{i}{r}")))
            for i in issues
            for r in (1, 2, 3)
        ]

    for name in ("a", "b"):
        write_run(root, f"seed-{name}", seeded(name))
    write_run(
        root,
        "ref",
        [
            (i, r, stepped(call(f"t{r}"), call("cat", n=r), say(f"{i}{r}")))
            for i in issues
            for r in (1, 2, 3)
        ],
    )


def test_seeded_runs_that_replay_beside_unseeded_ones_that_do_not_make_the_claim(
    spike: ModuleType, tmp_path: Path
) -> None:
    """Proves seeded runs that replay beside differing unseeded repeats make the replay claim."""
    build_study(tmp_path, replays=True)
    result = spike.report("seed-a", "seed-b", "ref")
    assert result["seeded"]["pairs"] == 24 and result["unseeded"]["pairs"] == 24
    assert result["seeded"]["first_action"]["k"] == 24
    assert result["unseeded"]["first_action"]["k"] == 0
    assert result["seeded"]["median_leading_steps"] == 3
    assert result["decision"]["a_seed_replays_a_campaign"] is True
    assert result["decision"]["plain_wilson_agrees"] is True
    assert result["difference"]["trajectory"][0] == pytest.approx(1.0)
    assert result["cells"]["seed-a"] == {
        "cells": 24,
        "missing_answers": 0,
        "no_answer_outcomes": 0,
        "unscorable": 0,
        "empty_transcripts": 0,
    }


def test_seeded_runs_that_agree_only_on_the_first_action_make_no_claim(
    spike: ModuleType, tmp_path: Path
) -> None:
    """Proves seeded runs agreeing only on the first action clear no trajectory, so no claim."""
    build_study(tmp_path, replays=False)
    result = spike.report("seed-a", "seed-b", "ref")
    assert result["seeded"]["first_action"]["k"] == 24 and result["seeded"]["trajectory"]["k"] == 0
    cleared = result["decision"]["measures_clearing_the_reference"]
    assert cleared["first_action"] is True and cleared["trajectory"] is False
    assert result["decision"]["failing"] == ["trajectory"]
    assert result["decision"]["a_seed_replays_a_campaign"] is False
