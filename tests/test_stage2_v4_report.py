"""The stage-2-on-v4 report: the four-point rule on synthetic rows, and every refusal.

Depends on: spikes/stage2_v4_report.py, pytest. Pure unit tests: invented issue and model names,
rows written under tmp_path, no GPU, no fixture, no network.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
IDS = [f"issue-{c}" for c in "abcdefg"]
MODELS = ["model-x", "model-y", "model-z"]
Row = dict[str, Any]


def _load() -> ModuleType:
    """Loads the spike script as a module (the spikes directory is not a package)."""
    path = ROOT / "spikes" / "stage2_v4_report.py"
    spec = importlib.util.spec_from_file_location("spike_stage2_v4_report", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rep = _load()


def model_rows(
    model: str,
    wins: int,
    version: str,
    scored_over: dict[tuple[str, int], Row] | None = None,
    raw_over: dict[tuple[str, int], Row] | None = None,
) -> tuple[list[Row], list[Row]]:
    """Scored and raw rows of one model: 3 repeats of each of the 7 issues, `wins` succeed first."""
    scored: list[Row] = []
    raw: list[Row] = []
    for n, (issue, repeat) in enumerate(itertools.product(IDS, (1, 2, 3))):
        trial_id = f"{version}-{model}-{issue}-{repeat}"
        won = n < wins
        row = {
            "trial_id": trial_id,
            "model": model,
            "issue": issue,
            "repeat": repeat,
            "success": won,
            "outcome": "fixed" if won else "missed",
            "answer_kind": "text",
            "trial_outcome": "completed",
            "tool_calls": 4,
        }
        rec = {
            "trial_id": trial_id,
            "model": model,
            "label": issue,
            "outcome": "completed",
            "steps": 5,
            "fixture_version": version,
            "fixture_profile": "dev",
        }
        scored.append({**row, **(scored_over or {}).get((issue, repeat), {})})
        raw.append({**rec, **(raw_over or {}).get((issue, repeat), {})})
    return scored, raw


def dump(path: Path, rows: list[Row]) -> None:
    """Writes rows as a JSON-lines file."""
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def write_rows(directory: Path, name: str, scored: list[Row], raw: list[Row]) -> Path:
    """Writes `name.scored.jsonl` and its raw sibling `name.jsonl`; returns the scored path."""
    dump(directory / f"{name}.jsonl", raw)
    dump(directory / f"{name}.scored.jsonl", scored)
    return directory / f"{name}.scored.jsonl"


def write_side(directory: Path, version: str, wins: dict[str, int], **over: Any) -> list[Path]:
    """One scored file (and raw sibling) per model for one side; `over` goes to `model_rows`."""
    directory.mkdir(parents=True, exist_ok=True)
    return [
        write_rows(directory, f"{version}-{m}", *model_rows(m, k, version, **over.get(m, {})))
        for m, k in wins.items()
    ]


def study(
    tmp_path: Path, v4: dict[str, int], v2: dict[str, int], **v4_over: Any
) -> tuple[list[Path], list[Path]]:
    """The v4 and v2 file lists of a synthetic study; `v4_over` maps a model to row overrides."""
    return write_side(tmp_path, "v4", v4, **v4_over), write_side(tmp_path, "v2", v2)


def run(tmp_path: Path, v4: dict[str, int], v2: dict[str, int], **v4_over: Any) -> Row:
    """The report of a synthetic study."""
    v4_paths, v2_paths = study(tmp_path, v4, v2, **v4_over)
    return rep.build_report(IDS, v4_paths, v2_paths)  # type: ignore[no-any-return]


def wins_of(a: int, b: int, c: int) -> dict[str, int]:
    """The three models' win counts out of 21."""
    return dict(zip(MODELS, (a, b, c), strict=True))


def tally_of(wins: int, scored: int = 21) -> Any:
    """A tally of `scored` scored trials, the first `wins` of them successes."""
    trials = [rep.Trial("m", f"i{n}", f"t{n}", n < wins, False, "") for n in range(scored)]
    return rep.tally(trials)


# --- the intervals -------------------------------------------------------------------------


def test_the_wilson_intervals_are_the_ones_the_plan_quotes() -> None:
    """19/21, 21/21 and 20/21 give the intervals the plan's comparator line prints."""
    got = [tally_of(k) for k in (19, 21, 20)]
    assert [(round(t.low, 2), round(t.high, 2)) for t in got] == [
        (0.71, 0.97),
        (0.85, 1.0),
        (0.77, 0.99),
    ]


def test_newcombe_reproduces_the_published_example() -> None:
    """Newcombe (1998), method 10, 56/70 against 48/80: 0.2000, interval 0.0524 to 0.3339."""
    diff, low, high = rep.newcombe(56, 70, 48, 80)
    assert (round(diff, 4), round(low, 4), round(high, 4)) == (0.2, 0.0524, 0.3339)


def test_newcombe_swaps_sign_and_stays_inside_minus_one_to_one() -> None:
    """Swapping the samples negates the difference and the interval; limits stay in [-1, 1]."""
    for n1, n2 in ((5, 5), (21, 21), (21, 63), (63, 20)):
        for k1 in range(0, n1 + 1, max(1, n1 // 7)):
            for k2 in range(0, n2 + 1, max(1, n2 // 7)):
                d, lo, hi = rep.newcombe(k1, n1, k2, n2)
                d2, lo2, hi2 = rep.newcombe(k2, n2, k1, n1)
                assert d == pytest.approx(-d2)
                assert (lo, hi) == pytest.approx((-hi2, -lo2))
                assert -1 - 1e-12 <= lo <= d <= hi <= 1 + 1e-12


def test_newcombe_of_equal_samples_straddles_zero_and_needs_trials() -> None:
    """Equal rates give a symmetric interval around 0; an empty side is an error."""
    d, lo, hi = rep.newcombe(10, 21, 10, 21)
    assert d == 0 and lo < 0 < hi and lo == pytest.approx(-hi)
    with pytest.raises(ValueError):
        rep.newcombe(0, 0, 3, 21)


# --- rule 1: stable or changed -------------------------------------------------------------


def test_overlapping_intervals_are_stable_disjoint_ones_changed_with_a_direction() -> None:
    """Rule 1 on three pairs of tallies: overlap, a fall and a rise."""
    assert rep.model_call(tally_of(21), tally_of(18)) == ("STABLE", "")
    assert rep.model_call(tally_of(21), tally_of(12)) == ("CHANGED", "lower on v4")
    assert rep.model_call(tally_of(12), tally_of(21)) == ("CHANGED", "higher on v4")


def test_intervals_that_only_touch_overlap_and_a_side_with_no_scored_trial_has_no_call() -> None:
    """Closed intervals sharing one end overlap; no scored trials means no call at all."""
    a = rep.Tally(21, 21, 10, {}, 0, 0, 0, 0.2, 0.5)
    b = rep.Tally(21, 21, 15, {}, 0, 0, 0, 0.5, 0.9)
    c = rep.Tally(21, 21, 20, {}, 0, 0, 0, 0.5 + 1e-3, 0.9)
    assert rep.model_call(a, b)[0] == "STABLE"
    assert rep.model_call(a, c)[0] == "CHANGED"
    assert rep.model_call(tally_of(0, 0), tally_of(10))[0] == "CANNOT DECIDE"


# --- the full rule on synthetic studies ----------------------------------------------------


def test_an_unchanged_study_is_stable_has_no_build_effect_and_the_order_holds(
    tmp_path: Path,
) -> None:
    """v4 equal to v2 for every model: STABLE x3, no build effect detectable, order HOLDS."""
    report = run(tmp_path, wins_of(19, 21, 20), wins_of(19, 21, 20))
    assert {m["call"] for m in report["models"].values()} == {"STABLE"}
    assert report["build_effect"]["difference"] == 0
    assert report["build_effect"]["contains_zero"]
    assert report["verdict"] == "HOLDS" and report["reasons"] == []
    assert [r["model"] for r in report["order"]["ranking"]] == ["model-y", "model-z", "model-x"]
    text = rep.render(report)
    assert rep.NO_EFFECT in text and "**The order HOLDS.**" in text
    assert "No pair is separated on v4" in text


def test_a_changed_model_means_the_order_does_not_hold(tmp_path: Path) -> None:
    """One model falling clear of its v2 interval makes it CHANGED and the order DOES NOT HOLD."""
    report = run(tmp_path, wins_of(8, 21, 20), wins_of(19, 21, 20))
    assert report["models"]["model-x"]["call"] == "CHANGED"
    assert report["models"]["model-x"]["direction"] == "lower on v4"
    assert report["verdict"] == "DOES NOT HOLD"
    assert report["reasons"][0] == "model-x is CHANGED (lower on v4)"
    assert "**The order DOES NOT HOLD.**" in rep.render(report)


def test_a_reversed_pair_breaks_the_order_even_when_every_model_is_stable(tmp_path: Path) -> None:
    """y ahead of x on v2, x clearly ahead of y on v4, both inside their own v2 intervals."""
    report = run(tmp_path, wins_of(19, 11, 15), wins_of(15, 16, 15))
    assert {m["call"] for m in report["models"].values()} == {"STABLE"}
    pairs = {(p["first"], p["second"]): p for p in report["order"]["pairs"]}
    assert pairs[("model-x", "model-y")]["excludes_zero"]
    assert pairs[("model-x", "model-y")]["reversed"]
    assert not pairs[("model-x", "model-z")]["excludes_zero"]
    assert report["verdict"] == "DOES NOT HOLD"
    assert report["reasons"] == [
        "model-x against model-y is separated on v4 opposite to its v2 point order"
    ]


def test_a_separated_pair_is_not_a_reversal_when_it_agrees_with_v2_or_v2_was_tied(
    tmp_path: Path,
) -> None:
    """Separation in v2's own direction, and separation where v2 had no order, do not reverse."""
    same_way = run(tmp_path / "a", wins_of(21, 12, 16), wins_of(19, 16, 16))
    pair = next(p for p in same_way["order"]["pairs"] if p["second"] == "model-y")
    assert pair["excludes_zero"] and pair["v2_order"] == 1 and not pair["reversed"]
    assert same_way["verdict"] == "HOLDS"
    tied = run(tmp_path / "b", wins_of(19, 11, 15), wins_of(16, 16, 16))
    pair = next(p for p in tied["order"]["pairs"] if p["second"] == "model-y")
    assert pair["excludes_zero"] and pair["v2_order"] == 0 and not pair["reversed"]
    assert tied["verdict"] == "HOLDS"
    assert "No pair is separated" not in rep.render(tied)


def test_the_ranking_is_by_point_estimate_and_equal_estimates_share_a_rank() -> None:
    """Competition ranking: 15, 15, 10 is ranks 1, 1, 3, names break the display order."""
    ranked = rep.ranking({"b": tally_of(15), "a": tally_of(15), "c": tally_of(10)})
    assert [(r["rank"], r["model"]) for r in ranked] == [(1, "a"), (1, "b"), (3, "c")]


def test_a_build_effect_is_named_only_when_the_interval_excludes_zero(tmp_path: Path) -> None:
    """The plain statement appears with a 0 inside the interval and never with 0 outside it."""
    quiet = rep.render(run(tmp_path / "a", wins_of(19, 21, 20), wins_of(19, 21, 20)))
    assert rep.NO_EFFECT in quiet
    drop = run(tmp_path / "b", wins_of(14, 14, 14), wins_of(21, 21, 21))
    assert not drop["build_effect"]["contains_zero"] and drop["build_effect"]["difference"] < 0
    text = rep.render(drop)
    assert rep.NO_EFFECT.lower() not in text.lower()
    assert "v4 is lower than v2" in text
    rise = run(tmp_path / "c", wins_of(21, 21, 21), wins_of(14, 14, 14))
    assert "v4 is higher than v2" in rep.render(rise)


def test_the_pooled_counts_and_the_difference_are_v4_minus_v2(tmp_path: Path) -> None:
    """Pooled v2 60/63 against pooled v4 57/63, difference -3/63, from the Newcombe function."""
    report = run(tmp_path, wins_of(18, 21, 18), wins_of(19, 21, 20))
    effect = report["build_effect"]
    assert (effect["v2"]["wins"], effect["v2"]["scored"]) == (60, 63)
    assert (effect["v4"]["wins"], effect["v4"]["scored"]) == (57, 63)
    assert (effect["difference"], effect["low"], effect["high"]) == pytest.approx(
        rep.newcombe(57, 63, 60, 63)
    )


# --- trials that cannot be scored, and missing answers --------------------------------------


def test_unscorable_and_harness_error_rows_are_counted_apart_not_as_failures(
    tmp_path: Path,
) -> None:
    """Both kinds stay in the 21 trials, leave the rate's numerator and denominator, and show."""
    over = {
        "model-x": {
            "scored_over": {
                ("issue-g", 3): {"outcome": "unscorable", "success": False},
                ("issue-g", 2): {"outcome": "harness_error", "success": False},
            }
        }
    }
    report = run(tmp_path, wins_of(19, 21, 20), wins_of(19, 21, 20), **over)
    x = report["models"]["model-x"]["v4"]
    assert (x["trials"], x["scored"], x["wins"]) == (21, 19, 19)
    assert x["unscored"] == {"unscorable": 1, "harness_error": 1, "infrastructure_fault": 0}
    text = rep.render(report)
    assert "v4 trials that cannot be scored: 2." in text
    assert "| model-x | v4 | 21 | 19 | 1 | 1 | 0 |" in text


def test_an_agent_error_with_no_steps_is_a_fault_but_one_with_steps_is_a_score(
    tmp_path: Path,
) -> None:
    """The registered infrastructure rule: agent_error and 0 steps is never a score."""
    over = {
        "model-x": {
            "scored_over": {
                ("issue-g", 3): {"trial_outcome": "agent_error"},
                ("issue-g", 2): {"trial_outcome": "agent_error"},
            },
            "raw_over": {
                ("issue-g", 3): {"outcome": "agent_error", "steps": 0},
                ("issue-g", 2): {"outcome": "agent_error", "steps": 9},
            },
        }
    }
    x = run(tmp_path, wins_of(19, 21, 20), wins_of(19, 21, 20), **over)["models"]["model-x"]["v4"]
    assert x["unscored"]["infrastructure_fault"] == 1
    assert (x["trials"], x["scored"], x["wins"]) == (21, 20, 19)


def test_missing_answers_are_counted_apart_from_wrong_ones(tmp_path: Path) -> None:
    """Empty and tool_json answers: counted among scored trials, split out of the failures."""
    over = {
        "model-x": {
            "scored_over": {
                ("issue-g", 3): {"answer_kind": "empty"},
                ("issue-g", 2): {"answer_kind": "tool_json"},
                ("issue-a", 1): {"answer_kind": "empty"},  # a success with no final message
            }
        }
    }
    report = run(tmp_path, wins_of(19, 21, 20), wins_of(19, 21, 20), **over)
    x = report["models"]["model-x"]["v4"]
    assert x["missing_answers"] == 3
    assert (x["failed_missing"], x["failed_wrong"]) == (2, 0)
    y = report["models"]["model-y"]["v4"]
    assert (y["missing_answers"], y["failed_missing"], y["failed_wrong"]) == (0, 0, 0)
    z = report["models"]["model-z"]["v4"]
    assert (z["failed_missing"], z["failed_wrong"]) == (0, 1)


def test_a_model_with_no_scored_trial_makes_the_result_undecidable_and_says_so(
    tmp_path: Path,
) -> None:
    """All 21 trials of a model unscorable: no intervals, no verdict, the report still renders."""
    every = {(i, r): {"outcome": "unscorable", "success": False} for i in IDS for r in (1, 2, 3)}
    report = run(
        tmp_path,
        wins_of(0, 21, 20),
        wins_of(19, 21, 20),
        **{"model-x": {"scored_over": every}},
    )
    assert report["verdict"] == "CANNOT DECIDE"
    assert report["build_effect"] is None and report["order"] is None
    text = rep.render(report)
    assert "CANNOT DECIDE" in text and "v4 trials that cannot be scored: 21." in text
    assert "**The order HOLDS" not in text and "DOES NOT HOLD" not in text


# --- inputs the study refuses --------------------------------------------------------------


def refused(
    tmp_path: Path,
    match: str,
    v4_over: dict[str, Any] | None = None,
    edit: Callable[[list[Path], list[Path]], None] | None = None,
) -> None:
    """Asserts that a synthetic study (row overrides, then a file edit) is refused as matching."""
    v4_paths, v2_paths = study(
        tmp_path, wins_of(19, 21, 20), wins_of(19, 21, 20), **(v4_over or {})
    )
    if edit:
        edit(v4_paths, v2_paths)
    with pytest.raises(rep.Refusal, match=match):
        rep.build_report(IDS, v4_paths, v2_paths)


def rewrite(path: Path, change: Any) -> None:
    """Reads a JSON-lines file, lets `change` edit the row list, and writes it back."""
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    path.write_text("".join(json.dumps(r) + "\n" for r in change(rows)))


def test_an_issue_missing_for_a_model_is_refused(tmp_path: Path) -> None:
    """All trials of one issue absent for one model: refused, naming the issue and the model."""

    def drop(v4_paths: list[Path], _v2: list[Path]) -> None:
        """Removes every trial of one issue from one model's v4 file."""
        rewrite(v4_paths[1], lambda rows: [r for r in rows if r["issue"] != "issue-c"])

    refused(tmp_path, r"issue issue-c is missing for model model-y", edit=drop)


@pytest.mark.parametrize(("extra", "count"), [(-1, 20), (1, 22)])
def test_a_model_with_fewer_or_more_than_21_trials_is_refused(
    tmp_path: Path, extra: int, count: int
) -> None:
    """20 and 22 trials on the seven issues are both refused, with the count in the message."""

    def change(v4_paths: list[Path], _v2: list[Path]) -> None:
        """Takes one trial from the first model's v4 file, or adds one."""
        scored_path = v4_paths[0]
        raw_path = scored_path.with_name(scored_path.name.replace(".scored", ""))
        if extra < 0:
            rewrite(scored_path, lambda rows: rows[:-1])
        else:
            more = {"trial_id": "extra-1", "issue": "issue-a", "model": "model-x", "success": True}
            rewrite(scored_path, lambda rows: [*rows, {**rows[0], **more}])
            rewrite(raw_path, lambda rows: [*rows, {**rows[0], "trial_id": "extra-1"}])

    refused(
        tmp_path, rf"has {count} trials on the 7 issues; the study fixes exactly 21", edit=change
    )


def test_three_repeats_of_every_issue_are_required_not_just_21_in_all(tmp_path: Path) -> None:
    """21 trials with one issue run 4 times and another twice are refused."""

    def swap(v4_paths: list[Path], _v2: list[Path]) -> None:
        """Moves one repeat of one issue to another in the third model's v4 file."""

        def change(rows: list[Row]) -> list[Row]:
            """Moves one repeat of issue-a to issue-b."""
            rows[0]["issue"] = "issue-b"
            return rows

        rewrite(v4_paths[2], change)

    refused(tmp_path, r"model model-z has 2 trials on issue issue-a, not 3", edit=swap)


def test_trials_on_other_issues_are_ignored_not_counted(tmp_path: Path) -> None:
    """A file may hold more issues than the seven; only the seven count toward the 21."""
    v4_paths, v2_paths = study(tmp_path, wins_of(19, 21, 20), wins_of(19, 21, 20))
    for path in (*v4_paths, *v2_paths):
        raw_path = path.with_name(path.name.replace(".scored", ""))
        rewrite(path, lambda rows: [*rows, {**rows[0], "trial_id": "o", "issue": "issue-other"}])
        rewrite(raw_path, lambda rows: [*rows, {**rows[0], "trial_id": "o"}])
    report = rep.build_report(IDS, v4_paths, v2_paths)
    assert report["models"]["model-x"]["v4"]["trials"] == 21
    assert report["inputs"]["v4"][0]["rows"] == 22 and report["inputs"]["v4"][0]["counted"] == 21


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("fixture_version", "v3", "fixture version 'v3', not 'v4'"),
        ("fixture_version", "v2", "fixture version 'v2', not 'v4'"),
        ("fixture_profile", "tune", "fixture profile 'tune', not 'dev'"),
    ],
)
def test_a_trial_on_another_version_or_profile_is_refused_from_either_record(
    tmp_path: Path, field: str, value: str, match: str
) -> None:
    """The raw record (which carries the fields) and the scored row (should it) are both checked."""
    over = {"model-y": {"raw_over": {("issue-d", 2): {field: value}}}}
    refused(tmp_path / "raw", match, v4_over=over)
    over = {"model-y": {"scored_over": {("issue-d", 2): {field: value}}}}
    refused(tmp_path / "scored", match, v4_over=over)


def test_the_v4_side_may_name_its_own_profile_and_only_that_one(tmp_path: Path) -> None:
    """The rerun on `dev+opencode-deps` needs `v4_profile`; the v2 side must still be `dev`."""
    deps = "dev+opencode-deps"
    over = {
        m: {"raw_over": {(i, r): {"fixture_profile": deps} for i in IDS for r in (1, 2, 3)}}
        for m in MODELS
    }
    v4_paths, v2_paths = study(tmp_path, wins_of(19, 21, 20), wins_of(19, 21, 20), **over)
    with pytest.raises(rep.Refusal, match="fixture profile 'dev\\+opencode-deps', not 'dev'"):
        rep.build_report(IDS, v4_paths, v2_paths)
    report = rep.build_report(IDS, v4_paths, v2_paths, v4_profile=deps)
    assert report["study"]["profile"] == deps
    assert report["study"]["comparator_profile"] == "dev"
    with pytest.raises(rep.Refusal, match="fixture profile 'dev', not 'dev\\+opencode-deps'"):
        rep.build_report(
            IDS,
            *study(tmp_path / "again", wins_of(19, 21, 20), wins_of(19, 21, 20)),
            v4_profile=deps,
        )


def test_the_comparator_must_be_v2(tmp_path: Path) -> None:
    """A v2 comparator file whose records say v4 (the wrong file passed) is refused."""

    def relabel(_v4: list[Path], v2_paths: list[Path]) -> None:
        """Marks every raw record of the first comparator file as a v4 trial."""
        raw_path = v2_paths[0].with_name(v2_paths[0].name.replace(".scored", ""))
        rewrite(raw_path, lambda rows: [{**r, "fixture_version": "v4"} for r in rows])

    refused(tmp_path, "fixture version 'v4', not 'v2'", edit=relabel)


def test_files_without_a_raw_sibling_or_a_trial_missing_from_it_are_refused(
    tmp_path: Path,
) -> None:
    """The fixture version lives in the raw record, so a scored file cannot stand alone."""
    v4_paths, v2_paths = study(tmp_path, wins_of(19, 21, 20), wins_of(19, 21, 20))
    raw_path = v4_paths[0].with_name(v4_paths[0].name.replace(".scored", ""))
    rewrite(raw_path, lambda rows: rows[1:])
    with pytest.raises(rep.Refusal, match="is not in the raw sibling"):
        rep.build_report(IDS, v4_paths, v2_paths)
    raw_path.unlink()
    with pytest.raises(rep.Refusal, match="cannot read"):
        rep.build_report(IDS, v4_paths, v2_paths)
    bare = tmp_path / "plain.jsonl"
    bare.write_text("")
    with pytest.raises(rep.Refusal, match=r"not a \*\.scored\.jsonl file"):
        rep.build_report(IDS, [bare], v2_paths)


def test_the_study_has_exactly_three_models_and_the_comparator_has_each_of_them(
    tmp_path: Path,
) -> None:
    """Two v4 models, or a v4 model with no v2 trials, are refused; extra v2 models are ignored."""
    v4_paths, v2_paths = study(tmp_path, wins_of(19, 21, 20), wins_of(19, 21, 20))
    with pytest.raises(rep.Refusal, match="2 models on these issues; the study has exactly 3"):
        rep.build_report(IDS, v4_paths[:2], v2_paths)
    with pytest.raises(rep.Refusal, match="no trials on these issues for model model-z"):
        rep.build_report(IDS, v4_paths, v2_paths[:2])
    extra = write_rows(tmp_path, "v2-model-w", *model_rows("model-w", 5, "v2"))
    report = rep.build_report(IDS, v4_paths, [*v2_paths, extra])
    assert report["inputs"]["ignored_v2_models"] == ["model-w"]
    assert list(report["models"]) == MODELS


def test_a_trial_counted_twice_or_a_bad_line_is_refused(tmp_path: Path) -> None:
    """The same file passed twice, and a line that is not JSON, never reach the numbers."""
    v4_paths, v2_paths = study(tmp_path, wins_of(19, 21, 20), wins_of(19, 21, 20))
    with pytest.raises(rep.Refusal, match="appears more than once"):
        rep.build_report(IDS, [*v4_paths, v4_paths[0]], v2_paths)
    v4_paths[1].write_text(v4_paths[1].read_text() + "{not json\n")
    with pytest.raises(rep.Refusal, match="not JSON"):
        rep.build_report(IDS, v4_paths, v2_paths)


@pytest.mark.parametrize(
    ("text", "match"),
    [
        (",".join(IDS[:6]), "6 ids given; the study has exactly 7"),
        (",".join([*IDS, "issue-h"]), "8 ids given"),
        (",".join([*IDS[:6], IDS[0]]), "an id is repeated"),
        (",".join([*IDS[:6], " "]), "an empty id"),
    ],
)
def test_the_issue_list_must_be_seven_distinct_ids(text: str, match: str) -> None:
    """The comma list is checked before any file is read."""
    with pytest.raises(rep.Refusal, match=match):
        rep.parse_ids(text)
    assert rep.parse_ids(", ".join(IDS)) == IDS


# --- the command line and the output -------------------------------------------------------


def argv(v4: list[Path], v2: list[Path], out: Path, ids: str | None = None) -> list[str]:
    """The command line of a run."""
    return [
        "--issues",
        ids or ",".join(IDS),
        "--v4",
        *map(str, v4),
        "--v2",
        *map(str, v2),
        "--out",
        str(out),
    ]


def test_main_prints_writes_the_markdown_and_the_same_numbers_as_json_beside_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 0; the .md is what was printed; the .json holds the numbers the tables show."""
    v4_paths, v2_paths = study(tmp_path, wins_of(19, 21, 20), wins_of(19, 21, 20))
    out = tmp_path / "out" / "report.md"
    assert rep.main(argv(v4_paths, v2_paths, out)) == 0
    printed = capsys.readouterr().out
    assert printed == out.read_text()
    data = json.loads(out.with_suffix(".json").read_text())
    assert data["verdict"] == "HOLDS"
    assert data["models"]["model-x"]["v4"]["wins"] == 19
    assert data["study"]["issues"] == IDS and data["study"]["trials_per_model"] == 21
    assert "| model-x | 19/21 | [0.71, 0.97] | 19/21 | [0.71, 0.97] | STABLE | - |" in printed
    assert rep.LIMITS in printed
    assert 'A "holds" verdict means "no change large enough to see", nothing more.' in printed
    assert not list(out.parent.glob("*.tmp"))


def test_the_markdown_names_no_issue_and_the_report_is_the_same_every_time(
    tmp_path: Path,
) -> None:
    """The markdown has the issue count and a digest, not the ids; two runs agree byte for byte."""
    v4_paths, v2_paths = study(tmp_path, wins_of(19, 21, 20), wins_of(19, 21, 20))
    first = rep.render(rep.build_report(IDS, v4_paths, v2_paths))
    second = rep.render(rep.build_report(IDS, v4_paths, v2_paths))
    assert first == second
    assert not any(i in first for i in IDS)
    assert "7 issues (set digest " in first


def test_one_combined_v4_file_gives_the_same_numbers_as_one_file_per_model(
    tmp_path: Path,
) -> None:
    """Models are told apart by the `model` field, not by the file."""
    v4_paths, v2_paths = study(tmp_path / "split", wins_of(19, 21, 20), wins_of(19, 21, 20))
    scored: list[Row] = []
    raw: list[Row] = []
    for m, k in wins_of(19, 21, 20).items():
        s, r = model_rows(m, k, "v4")
        scored += s
        raw += r
    (tmp_path / "joint").mkdir()
    combined = write_rows(tmp_path / "joint", "v4-all", scored, raw)
    one = rep.build_report(IDS, v4_paths, v2_paths)
    other = rep.build_report(IDS, [combined], v2_paths)
    for key in ("models", "pooled", "build_effect", "order", "verdict"):
        assert one[key] == other[key]


def test_a_refusal_exits_1_with_a_message_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both a bad id list and a bad file stop with `refused:` on stderr and no output files."""
    v4_paths, v2_paths = study(tmp_path, wins_of(19, 21, 20), wins_of(19, 21, 20))
    out = tmp_path / "r.md"
    assert rep.main(argv(v4_paths, v2_paths, out, ids=",".join(IDS[:5]))) == 1
    assert "refused: --issues: 5 ids given" in capsys.readouterr().err
    assert rep.main(argv(v4_paths[:2], v2_paths, out)) == 1
    assert "refused:" in capsys.readouterr().err
    assert rep.main(argv(v4_paths, v2_paths, tmp_path / "r.txt")) == 1
    assert "--out must end in .md" in capsys.readouterr().err
    assert not out.exists() and not out.with_suffix(".json").exists()
    assert not (tmp_path / "r.txt").exists()


def test_the_script_names_no_issue_id_and_no_digest() -> None:
    """The script is published, the ids are private: none is written in it, nor any digest."""
    source = (ROOT / "spikes" / "stage2_v4_report.py").read_text()
    assert not re.search(r"\b(?:fix|mut|hand)-[A-Za-z0-9_]", source)
    assert not re.search(r"\b[0-9a-f]{12,}\b", source)
