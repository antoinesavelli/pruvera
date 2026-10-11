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


def tool(name: str) -> tuple[str, str]:
    """Returns the (tool, name) action pair for a tool call."""
    return ("tool", name)


def text(body: str) -> tuple[str, str]:
    """Returns the (text, body) action pair for a text part."""
    return ("text", body)


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
        ([], [], False, True, 0),
    ],
)
def test_the_registered_measures_for_one_pair(
    spike: ModuleType, a: list[Any], b: list[Any], first: bool, same_trajectory: bool, lead: int
) -> None:
    """Proves `measure` gives the first-action, trajectory, outcome and leading values per pair."""
    got = spike.measure(spike.Cell(a, "fixed"), spike.Cell(b, "fixed"))
    assert got == {
        "first_action": first,
        "trajectory": same_trajectory,
        "outcome": True,
        "leading": lead,
    }


def test_a_different_scored_outcome_is_outcome_disagreement_alone(spike: ModuleType) -> None:
    """Proves equal actions with different scored outcomes fail only the outcome measure."""
    got = spike.measure(spike.Cell([tool("ls")], "fixed"), spike.Cell([tool("ls")], "missed"))
    assert got["trajectory"] and got["first_action"] and not got["outcome"]


def write_run(root: Path, name: str, trials: list[tuple[str, int, list[Event]]]) -> None:
    """One run: (issue, repeat, events) per trial, all scored `fixed`."""
    results, scored = [], []
    for n, (issue, repeat, events) in enumerate(trials):
        tid = f"{name}-{n}"
        (root / "artifacts" / tid).mkdir(parents=True)
        (root / "artifacts" / tid / "transcript.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in events)
        )
        results.append(
            {"trial_id": tid, "label": issue, "repeat": repeat, "outcome": "completed", "steps": 2}
        )
        scored.append({"trial_id": tid, "outcome": "fixed"})
    (root / "experiments").mkdir(exist_ok=True)
    for suffix, rows in ((".jsonl", results), (".scored.jsonl", scored)):
        (root / "experiments" / f"{name}{suffix}").write_text(
            "".join(json.dumps(r) + "\n" for r in rows)
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


def test_reference_pairs_are_the_repeat_pairs_of_each_issue_and_seeded_pairs_the_shared_cells(
    spike: ModuleType,
) -> None:
    """Proves reference pairs are each issue's repeat pairs and seeded pairs the shared cells."""
    cell = spike.Cell([tool("ls")], "x")
    run = {(i, r): cell for i in ("a", "b") for r in (1, 2, 3)}
    assert [issue for issue, _ in spike.reference_pairs(run)] == ["a"] * 3 + ["b"] * 3
    assert len(spike.reference_pairs({k: v for k, v in run.items() if k[1] < 3})) == 2
    fewer = {k: v for k, v in run.items() if k != ("a", 2)}
    assert [issue for issue, _ in spike.seeded_pairs(run, fewer)] == ["a", "a", "b", "b", "b"]


def part(k: int, n: int) -> dict[str, Any]:
    """Builds a summary dict with k, n and the Wilson interval for k successes in n."""
    return {"k": k, "n": n, "wilson": list(stats.wilson(k, n))}


def test_the_claim_needs_separated_intervals_and_a_gap_of_a_fifth(spike: ModuleType) -> None:
    """Proves `exceeds` needs non-overlapping Wilson intervals and a gap of at least 0.2."""
    assert spike.exceeds(part(90, 93), part(10, 93))
    assert not spike.exceeds(part(45, 93), part(40, 93)), "the intervals overlap"
    # separated intervals (large n) but a gap of only 0.1, and a gap under 0.2 is not claimed
    seeded, reference = part(300, 500), part(250, 500)
    assert seeded["wilson"][0] > reference["wilson"][1]
    assert not spike.exceeds(seeded, reference)


def build_study(root: Path, replays: bool) -> None:
    """Eight issues, three repeats. Seeded run b matches a (or drifts after the first call);
    unseeded repeats differ from the first call on."""
    issues = [f"i{n}" for n in range(8)]

    def seeded(name: str) -> list[tuple[str, int, list[Event]]]:
        """Lists a seeded run's trials; run b alters its second call unless replaying."""
        drift = 0 if replays or name == "a" else 1
        return [
            (i, r, [call("ls", d=i), call("cat", n=r + drift), say(f"{i}{r}")])
            for i in issues
            for r in (1, 2, 3)
        ]

    for name in ("a", "b"):
        write_run(root, f"seed-{name}", seeded(name))
    write_run(
        root,
        "ref",
        [
            (i, r, [call(f"t{r}"), call("cat", n=r), say(f"{i}{r}")])
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
    assert result["seeded"]["median_leading_actions"] == 3
    assert result["decision"]["a_seed_replays_a_campaign"] is True
    assert result["difference"]["trajectory"][0] == pytest.approx(1.0)


def test_seeded_runs_that_agree_only_on_the_first_action_make_no_claim(
    spike: ModuleType, tmp_path: Path
) -> None:
    """Proves seeded runs agreeing only on the first action clear no trajectory, so no claim."""
    build_study(tmp_path, replays=False)
    result = spike.report("seed-a", "seed-b", "ref")
    assert result["seeded"]["first_action"]["k"] == 24 and result["seeded"]["trajectory"]["k"] == 0
    cleared = result["decision"]["measures_clearing_the_reference"]
    assert cleared == {"first_action": True, "trajectory": False}
    assert result["decision"]["a_seed_replays_a_campaign"] is False
