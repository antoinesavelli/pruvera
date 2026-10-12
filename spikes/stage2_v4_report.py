"""The report for the stage-2-on-fixture-v4 study: its four-point decision rule, computed.

Depends on: bench.stats (the Wilson interval) and the standard library. Run from the repo root once
the v4 trials are scored. The inputs are one v4 scored file per model (or one combined file) and the
recorded v2 stage-2 scored files as the comparator; each scored file must sit beside its raw
sibling (`X.jsonl` for `X.scored.jsonl`), because only the raw records carry the fixture version
and profile:
    python3 -B spikes/stage2_v4_report.py --issues ID,ID,ID,ID,ID,ID,ID \\
        --v4 A.scored.jsonl [B.scored.jsonl ...] --v2 V2A.scored.jsonl [...] --out REPORT.md
It prints the report as markdown, writes it to REPORT.md and the same numbers to REPORT.json.

The rule, as pre-registered (the fixture-v3 plan, section 6; fixed before any v4 trial):
  1. STABLE: a model's v4 Wilson interval overlaps its own v2 interval on the same issues, else
     CHANGED, with the direction.
  2. Build effect: the pooled v4 rate over the models against the pooled v2 rate, with a Newcombe
     interval for the difference; "no build effect detectable" if it contains 0.
  3. Order: the v4 ranking by point estimate and, for each pair, whether the Newcombe interval for
     the difference excludes 0. The order HOLDS if every model is STABLE and no pair is separated
     on v4 in the opposite direction to its v2 point order.
  4. A trial that cannot be scored is reported separately. A result that cannot decide says so.
The issue ids are given on the command line and no id or model name is written in this file. The
script refuses (exit 1, a message on stderr, nothing written) if a model does not have exactly
3 trials on each of the 7 issues, if an issue is missing for a model, if the v4 files do not hold
exactly 3 models or a v2 file lacks one of them, or if a record names another fixture version or
profile than the study's.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import itertools
import json
import math
import operator
import os
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench.stats import Z95, wilson  # noqa: E402

Row = dict[str, Any]
Report = dict[str, Any]

V4, V2 = "v4", "v2"  # the build under test and the comparator's build
PROFILE = "dev"  # the profile both sides ran on
N_ISSUES = 7  # "the seven still-applicable test-caught fix issues"
REPEATS = 3  # "n = 3 each"
N_MODELS = 3  # "three models"
MISSING = ("empty", "tool_json")  # `answer_kind`: no answer at all, counted apart from a wrong one
EPS = 1e-9  # two intervals that touch at an end (to rounding) overlap
LIMITS = 'A "holds" verdict means "no change large enough to see", nothing more.'
NO_EFFECT = "No build effect detectable"
UNSCORED_KINDS = ("unscorable", "harness_error", "infrastructure_fault")
no_answer = operator.attrgetter("missing_answer")  # a trial with no final message


class Refusal(Exception):
    """The inputs are not what the pre-registered study requires; nothing is reported."""


@dataclass(frozen=True)
class Trial:
    """One counted trial: whose it is, whether it succeeded, and why it was not scored if so."""

    model: str
    issue: str
    trial_id: str
    success: bool
    missing_answer: bool
    unscored: str  # empty for a scored trial, else one of UNSCORED_KINDS


@dataclass(frozen=True)
class Tally:
    """One model's (or the pool's) trials on one side: the counts, apart counts and the interval."""

    trials: int
    scored: int
    wins: int
    unscored: dict[str, int]
    missing_answers: int  # scored trials with no answer (`answer_kind` empty or tool_json)
    failed_missing: int  # scored failures that gave no answer
    failed_wrong: int  # scored failures that gave an answer
    low: float  # Wilson 95%, over the scored trials
    high: float


def read_jsonl(path: Path) -> list[Row]:
    """The JSON-object lines of a results file; an unreadable file or bad line is a refusal."""
    try:
        text = path.read_text()
    except OSError as exc:
        raise Refusal(f"cannot read {path}: {exc}") from exc
    rows: list[Row] = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise Refusal(f"{path.name} line {number}: not JSON ({exc})") from exc
        if not isinstance(row, dict):
            raise Refusal(f"{path.name} line {number}: not a JSON object")
        rows.append(row)
    return rows


def raw_sibling(path: Path) -> Path:
    """The raw trials file beside a scored file (`X.jsonl` for `X.scored.jsonl`)."""
    if ".scored" not in path.name:
        raise Refusal(f"{path.name}: not a *.scored.jsonl file, so it has no raw sibling")
    return path.with_name(path.name.replace(".scored", "", 1))


def need(row: Row, key: str, where: str) -> Any:
    """A field a row must carry, or a refusal naming the row."""
    if key not in row:
        raise Refusal(f"{where}: no `{key}` field")
    return row[key]


def check_provenance(record: Row, version: str, where: str) -> None:
    """Refuses a record that carries a fixture version or profile other than the study's."""
    # "if a trial's profile or fixture version is not v4 when the row carries that field": a
    # scored row carries neither, a raw trial record carries both, so both records are checked.
    if "fixture_version" in record and record["fixture_version"] != version:
        raise Refusal(f"{where}: fixture version {record['fixture_version']!r}, not {version!r}")
    if "fixture_profile" in record and record["fixture_profile"] != PROFILE:
        raise Refusal(f"{where}: fixture profile {record['fixture_profile']!r}, not {PROFILE!r}")


def unscored_reason(row: Row, raw: Row, where: str) -> str:
    """Why a trial cannot be scored (else empty): unscorable, a harness error or a fault."""
    outcome = need(row, "outcome", where)
    if outcome in ("unscorable", "harness_error"):
        return str(outcome)
    # Registered with the study: "an agent_error with 0 steps is a fault, never a score".
    ended = raw.get("outcome", row.get("trial_outcome"))
    return "infrastructure_fault" if ended == "agent_error" and not raw.get("steps") else ""


def to_trial(row: Row, raw_by_id: dict[str, Row], version: str, where: str) -> Trial:
    """One scored row, joined to its raw record and checked, as a counted trial."""
    trial_id = str(need(row, "trial_id", where))
    raw = raw_by_id.get(trial_id)
    if raw is None:
        raise Refusal(f"{where}: trial {trial_id} is not in the raw sibling file")
    check_provenance(row, version, where)
    check_provenance(raw, version, where)
    return Trial(
        model=str(need(row, "model", where)),
        issue=str(need(row, "issue", where)),
        trial_id=trial_id,
        success=bool(need(row, "success", where)),
        missing_answer=row.get("answer_kind", "") in MISSING,
        unscored=unscored_reason(row, raw, where),
    )


def load_group(
    paths: Sequence[Path], version: str, ids: Sequence[str]
) -> tuple[list[Trial], list[Row]]:
    """The counted trials (rows on `ids`) of one side's files, and a record of each input file."""
    wanted = set(ids)
    trials: list[Trial] = []
    inputs: list[Row] = []
    for path in paths:
        scored = read_jsonl(path)
        raw_by_id = {str(r.get("trial_id")): r for r in read_jsonl(raw_sibling(path))}
        kept = 0
        for number, row in enumerate(scored, 1):
            where = f"{path.name} row {number}"
            if need(row, "issue", where) not in wanted:
                continue
            trials.append(to_trial(row, raw_by_id, version, where))
            kept += 1
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        inputs.append({"file": path.name, "rows": len(scored), "counted": kept, "sha256": digest})
    return trials, inputs


def by_model(trials: Sequence[Trial]) -> dict[str, list[Trial]]:
    """The trials grouped by model, models in alphabetical order."""
    grouped: dict[str, list[Trial]] = collections.defaultdict(list)
    for trial in trials:
        grouped[trial.model].append(trial)
    return dict(sorted(grouped.items()))


def check_unique(trials: Sequence[Trial], label: str) -> None:
    """Refuses a side in which one trial id is counted twice (a file passed twice, say)."""
    seen = collections.Counter(t.trial_id for t in trials)
    twice = sorted(i for i, n in seen.items() if n > 1)
    if twice:
        raise Refusal(f"{label}: trial {twice[0]} appears more than once ({len(twice)} such)")


def check_counts(grouped: dict[str, list[Trial]], ids: Sequence[str], label: str) -> None:
    """Refuses a model without exactly REPEATS trials on every issue (REPEATS x len(ids) in all)."""
    expected = REPEATS * len(ids)
    for model, trials in grouped.items():
        per_issue = collections.Counter(t.issue for t in trials)
        for issue in ids:
            if per_issue[issue] == 0:
                raise Refusal(f"{label}: issue {issue} is missing for model {model}")
        if len(trials) != expected:
            raise Refusal(
                f"{label}: model {model} has {len(trials)} trials on the {len(ids)} issues; "
                f"the study fixes exactly {expected} ({REPEATS} x {len(ids)})"
            )
        for issue in ids:
            if per_issue[issue] != REPEATS:
                raise Refusal(
                    f"{label}: model {model} has {per_issue[issue]} trials on issue {issue}, "
                    f"not {REPEATS}"
                )


def parse_ids(text: str) -> list[str]:
    """The issue ids of a comma list: exactly N_ISSUES, none blank, none repeated."""
    ids = [part.strip() for part in text.split(",")]
    if any(not i for i in ids):
        raise Refusal("--issues: an empty id in the comma list")
    if len(set(ids)) != len(ids):
        raise Refusal("--issues: an id is repeated")
    if len(ids) != N_ISSUES:
        raise Refusal(f"--issues: {len(ids)} ids given; the study has exactly {N_ISSUES}")
    return ids


def tally(trials: Sequence[Trial]) -> Tally:
    """The counts of a set of trials; rates and the Wilson interval use the scored trials only."""
    scored = [t for t in trials if not t.unscored]
    failed = [t for t in scored if not t.success]
    wins = len(scored) - len(failed)
    low, high = wilson(wins, len(scored))
    kinds = collections.Counter(t.unscored for t in trials)
    failed_missing = sum(map(no_answer, failed))
    return Tally(
        trials=len(trials),
        scored=len(scored),
        wins=wins,
        unscored={kind: kinds[kind] for kind in UNSCORED_KINDS},
        missing_answers=sum(map(no_answer, scored)),
        failed_missing=failed_missing,
        failed_wrong=len(failed) - failed_missing,
        low=low,
        high=high,
    )


def pooled(grouped: dict[str, list[Trial]]) -> Tally:
    """The tally of every model's trials together."""
    return tally(list(itertools.chain.from_iterable(grouped.values())))


def newcombe(k1: int, n1: int, k2: int, n2: int) -> tuple[float, float, float]:
    """(p1 - p2, lower, upper): Newcombe's hybrid score interval for two independent proportions."""
    # Newcombe RG (1998), Statistics in Medicine 17:873-890, method 10: the Wilson limits (l, u) of
    # each proportion, combined as a square-and-add. It treats the two samples as independent.
    if n1 <= 0 or n2 <= 0:
        raise ValueError("a Newcombe interval needs trials on both sides")
    p1, p2 = k1 / n1, k2 / n2
    l1, u1 = wilson(k1, n1)
    l2, u2 = wilson(k2, n2)
    diff = p1 - p2
    lower = diff - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2)
    upper = diff + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)
    return diff, lower, upper


def overlaps(a: Tally, b: Tally) -> bool:
    """Whether two closed intervals share a point (touching at an end counts)."""
    return a.low <= b.high + EPS and b.low <= a.high + EPS


def model_call(v2: Tally, v4: Tally) -> tuple[str, str]:
    """Rule 1: ("STABLE", "") or ("CHANGED", direction); no scored trials on a side: no call."""
    # "a model is STABLE if its v3 Wilson interval overlaps its own v2 interval on the same seven
    # issues; otherwise CHANGED, with the direction reported." The plan's "v3" is the build under
    # test, which the owner's 2026-10-11 decision made v4 (the section's own title says v4).
    if v2.scored == 0 or v4.scored == 0:
        return "CANNOT DECIDE", ""
    if overlaps(v2, v4):
        return "STABLE", ""
    return "CHANGED", "higher on v4" if v4.low > v2.high else "lower on v4"


def build_effect(v2: Tally, v4: Tally) -> Row:
    """Rule 2: the pooled v4 rate against the pooled v2 rate, with a Newcombe interval."""
    # "the pooled v3 rate over the three models against 60/63, with a Newcombe interval for the
    # difference; 'no build effect detectable' if it contains 0." The difference is v4 minus v2;
    # the two sides are different runs on different builds, so the samples are independent.
    diff, low, high = newcombe(v4.wins, v4.scored, v2.wins, v2.scored)
    return {
        "v2": asdict(v2),
        "v4": asdict(v4),
        "difference": diff,
        "low": low,
        "high": high,
        "contains_zero": low <= 0 <= high,
    }


def sign(value: Fraction | float) -> int:
    """-1, 0 or 1."""
    return (value > 0) - (value < 0)


def ranking(v4: dict[str, Tally]) -> list[Row]:
    """The models by v4 point estimate, highest first; equal estimates share a rank."""
    rates = {m: Fraction(t.wins, t.scored) for m, t in v4.items()}
    ordered = sorted(v4, key=lambda m: (-rates[m], m))
    rows: list[Row] = []
    rank = 0
    for position, model in enumerate(ordered, 1):
        if position == 1 or rates[model] != rates[ordered[position - 2]]:
            rank = position
        t = v4[model]
        rows.append(
            {
                "rank": rank,
                "model": model,
                "wins": t.wins,
                "scored": t.scored,
                "rate": t.wins / t.scored,
            }
        )
    return rows


def pair_row(a: str, b: str, v2: dict[str, Tally], v4: dict[str, Tally]) -> Row:
    """Rule 3 for one pair (a ranked at or above b): the v4 Newcombe interval and the v2 order."""
    # "for each pair, whether the Newcombe interval for the difference excludes 0 ... no pair is
    # separated on v3 in the opposite direction to its v2 point order." Separated: the interval
    # excludes 0. The v2 point order is the sign of the v2 rate difference; a v2 tie has no order,
    # so such a pair cannot be reversed. Independent samples are assumed (conservative: both
    # models ran the same issues, which a paired interval would use).
    diff, low, high = newcombe(v4[a].wins, v4[a].scored, v4[b].wins, v4[b].scored)
    separation = 1 if low > 0 else -1 if high < 0 else 0
    v2_order = sign(Fraction(v2[a].wins, v2[a].scored) - Fraction(v2[b].wins, v2[b].scored))
    return {
        "first": a,
        "second": b,
        "difference": diff,
        "low": low,
        "high": high,
        "excludes_zero": separation != 0,
        "v2_order": v2_order,
        "reversed": separation != 0 and v2_order != 0 and separation != v2_order,
    }


def order_section(
    models: Sequence[str], v2: dict[str, Tally], v4: dict[str, Tally], calls: dict[str, Row]
) -> Row:
    """Rule 3: the v4 ranking, every pair's interval, and the HOLDS / DOES NOT HOLD call."""
    ranked = ranking(v4)
    names = [r["model"] for r in ranked]
    pairs = [pair_row(a, b, v2, v4) for i, a in enumerate(names) for b in names[i + 1 :]]
    reasons = [
        f"{m} is CHANGED ({calls[m]['direction']})" for m in models if calls[m]["call"] == "CHANGED"
    ]
    reasons += [
        f"{p['first']} against {p['second']} is separated on v4 opposite to its v2 point order"
        for p in pairs
        if p["reversed"]
    ]
    return {
        "ranking": ranked,
        "pairs": pairs,
        "verdict": "DOES NOT HOLD" if reasons else "HOLDS",
        "reasons": reasons,
        "any_pair_separated": any(p["excludes_zero"] for p in pairs),
    }


def model_entries(t2: dict[str, Tally], t4: dict[str, Tally]) -> dict[str, Row]:
    """Rule 1 for every model: both tallies, the call and its direction."""
    entries: dict[str, Row] = {}
    for model, v4 in t4.items():
        call, direction = model_call(t2[model], v4)
        entries[model] = {"v2": asdict(t2[model]), "v4": asdict(v4), "call": call}
        entries[model]["direction"] = direction
    return entries


def analyse(v4: dict[str, list[Trial]], v2: dict[str, list[Trial]]) -> Report:
    """The four points over the models' trials: per-model calls, build effect, order, counts."""
    models = list(v4)
    t4 = {m: tally(v4[m]) for m in models}
    t2 = {m: tally(v2[m]) for m in models}
    pooled4, pooled2 = pooled(v4), pooled(v2)
    calls = model_entries(t2, t4)
    decidable = all(t.scored > 0 for t in (*t4.values(), *t2.values()))
    report: Report = {
        "models": calls,
        "pooled": {"v2": asdict(pooled2), "v4": asdict(pooled4)},
        "build_effect": None,
        "order": None,
        "verdict": "CANNOT DECIDE",
        "reasons": [] if decidable else ["a model has no scored trials on one side"],
        "limits": LIMITS,
    }
    if decidable:
        report["build_effect"] = build_effect(pooled2, pooled4)
        report["order"] = order_section(models, t2, t4, calls)
        report["verdict"] = report["order"]["verdict"]
        report["reasons"] = report["order"]["reasons"]
    return report


def build_report(ids: Sequence[str], v4_paths: Sequence[Path], v2_paths: Sequence[Path]) -> Report:
    """Loads and checks both sides, then computes the report; a refusal is raised, not printed."""
    v4_trials, v4_inputs = load_group(v4_paths, V4, ids)
    v2_trials, v2_inputs = load_group(v2_paths, V2, ids)
    check_unique(v4_trials, V4)
    check_unique(v2_trials, "v2 comparator")
    v4, v2_all = by_model(v4_trials), by_model(v2_trials)
    if len(v4) != N_MODELS:
        raise Refusal(f"{V4}: {len(v4)} models on these issues; the study has exactly {N_MODELS}")
    absent = [m for m in v4 if m not in v2_all]
    if absent:
        raise Refusal(f"v2 comparator: no trials on these issues for model {absent[0]}")
    v2 = {m: v2_all[m] for m in v4}
    check_counts(v4, ids, V4)
    check_counts(v2, ids, "v2 comparator")
    report = analyse(v4, v2)
    report["study"] = {
        "version": V4,
        "comparator_version": V2,
        "profile": PROFILE,
        "issues": list(ids),
        "issue_set_digest": hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()[:12],
        "repeats": REPEATS,
        "models": list(v4),
        "trials_per_model": REPEATS * len(ids),
        "confidence": 0.95,
        "z": Z95,
    }
    report["inputs"] = {
        "v4": v4_inputs,
        "v2": v2_inputs,
        "ignored_v2_models": sorted(set(v2_all) - set(v4)),
    }
    return report


def rate(t: Row) -> str:
    """`k/n` over the scored trials of a tally dict."""
    return f"{t['wins']}/{t['scored']}"


def interval(low: float, high: float, digits: int = 2) -> str:
    """`[low, high]` to `digits` decimals."""
    return f"[{low:.{digits}f}, {high:.{digits}f}]"


def models_section(report: Report) -> list[str]:
    """Rule 1: the table of per-model v2 and v4 counts, Wilson intervals and calls."""
    lines = [
        "## 1. Stable or changed, per model (Wilson 95%)",
        "",
        "| model | v2 | v2 interval | v4 | v4 interval | call | direction |",
        "|---|---|---|---|---|---|---|",
    ]
    for model, m in report["models"].items():
        v2, v4 = m["v2"], m["v4"]
        lines.append(
            f"| {model} | {rate(v2)} | {interval(v2['low'], v2['high'])} | {rate(v4)} "
            f"| {interval(v4['low'], v4['high'])} | {m['call']} | {m['direction'] or '-'} |"
        )
    lines += [
        "",
        "A model is STABLE if its v4 interval overlaps its own v2 interval, else CHANGED.",
    ]
    short = [mod for mod, m in report["models"].items() if m["v4"]["scored"] < m["v4"]["trials"]]
    if short:
        lines.append(
            "Scored v4 trials are fewer than counted for: " + ", ".join(short) + " (section 4)."
        )
    return lines + [""]


def effect_section(report: Report) -> list[str]:
    """Rule 2: the pooled rates, the Newcombe interval and the plain statement."""
    lines = ["## 2. Build effect (pooled over the models)", ""]
    effect = report["build_effect"]
    if effect is None:
        return [*lines, "CANNOT DECIDE: a model has no scored trials on one side.", ""]
    v2, v4 = effect["v2"], effect["v4"]
    lines.append(
        f"Pooled v2 {rate(v2)} {interval(v2['low'], v2['high'])}; pooled v4 {rate(v4)} "
        f"{interval(v4['low'], v4['high'])} (Wilson 95%)."
    )
    lines.append(
        f"Difference, v4 minus v2: {effect['difference']:+.3f}, Newcombe 95% interval "
        f"{interval(effect['low'], effect['high'], 3)} (independent samples assumed)."
    )
    if effect["contains_zero"]:
        lines.append(f"**{NO_EFFECT}**: the interval contains 0.")
    else:
        way = "higher" if effect["difference"] > 0 else "lower"
        lines.append(f"A build effect is detectable: the interval excludes 0, v4 is {way} than v2.")
    return [*lines, ""]


def order_lines(order: Row) -> list[str]:
    """Rule 3: the ranking line and the table of pairs."""
    shared = collections.Counter(r["rank"] for r in order["ranking"])
    ranked = [
        f"{r['rank']}{'=' if shared[r['rank']] > 1 else '.'} {r['model']} "
        f"({r['wins']}/{r['scored']})"
        for r in order["ranking"]
    ]
    lines = ["v4 ranking by point estimate: " + "; ".join(ranked) + ".", ""]
    lines += [
        "| first | second | v4 difference (first minus second) | Newcombe 95% | excludes 0 "
        "| v2 point order | against it |",
        "|---|---|---|---|---|---|---|",
    ]
    words = {1: "first ahead", -1: "second ahead", 0: "tied"}
    for p in order["pairs"]:
        lines.append(
            f"| {p['first']} | {p['second']} | {p['difference']:+.3f} "
            f"| {interval(p['low'], p['high'], 3)} | {'yes' if p['excludes_zero'] else 'no'} "
            f"| {words[p['v2_order']]} | {'REVERSED' if p['reversed'] else 'no'} |"
        )
    return lines


def order_section_lines(report: Report) -> list[str]:
    """Rule 3 as printed: ranking, pairs, and the call by the rule's words."""
    lines = ["## 3. Order", ""]
    order = report["order"]
    if order is None:
        return [*lines, "**CANNOT DECIDE**: a model has no scored trials on one side.", ""]
    lines += order_lines(order)
    lines += [
        "",
        "The order HOLDS if every model is STABLE and no pair is separated on v4 in the opposite "
        "direction to its v2 point order.",
        "",
        f"**The order {report['verdict']}.**",
    ]
    if report["reasons"]:
        lines += ["", *[f"- {reason}" for reason in report["reasons"]]]
    if not order["any_pair_separated"]:
        lines += [
            "",
            "No pair is separated on v4, so the v4 order itself is not resolved; "
            "HOLDS here means only that no reversal was detectable.",
        ]
    return [*lines, ""]


def apart_section(report: Report) -> list[str]:
    """Rule 4: trials that cannot be scored, and missing answers, each reported apart."""
    lines = [
        "## 4. Reported apart: trials that cannot be scored, and missing answers",
        "",
        "| model | side | trials | scored | unscorable | harness error | infrastructure fault "
        "| missing answers (of scored) | failures, wrong answer | failures, missing answer |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for model, m in report["models"].items():
        for side in ("v4", "v2"):
            t = m[side]
            u = t["unscored"]
            lines.append(
                f"| {model} | {side} | {t['trials']} | {t['scored']} | {u['unscorable']} "
                f"| {u['harness_error']} | {u['infrastructure_fault']} | {t['missing_answers']} "
                f"| {t['failed_wrong']} | {t['failed_missing']} |"
            )
    cannot = sum(sum(m["v4"]["unscored"].values()) for m in report["models"].values())
    lines += [
        "",
        f"v4 trials that cannot be scored: {cannot}. They are neither a success nor a failure: the "
        "rates above count the scored trials only. An unscorable row, a harness error and an "
        "`agent_error` with 0 steps (a fault, never a score) are the three kinds. A missing answer "
        "is an `answer_kind` of `empty` or `tool_json`; a failure that gave an answer is a wrong "
        "one.",
        "",
    ]
    return lines


def method_section(report: Report) -> list[str]:
    """What was assumed, the inputs, and the limits sentence."""
    study = report["study"]
    lines = ["## Method and limits", ""]
    shape = (
        f"profile {study['profile']} of fixture {study['version']}, {len(study['issues'])} issues "
        f"(set digest {study['issue_set_digest']}), {study['repeats']} repeats, "
        f"{len(study['models'])} models, {study['trials_per_model']} trials a model"
    )
    lines += [
        f"- Exploratory; nothing routes from it. {shape.capitalize()}; compared with the recorded "
        f"{study['comparator_version']} stage-2 trials on the same issues.",
        "- Success is the scored row's `success`. Wilson and Newcombe intervals are 95% and count "
        "trials, not issues: the trials of one issue are not independent, which neither interval "
        "allows for.",
        "- Differences are Newcombe's hybrid score interval (Newcombe 1998, method 10) on "
        "independent samples: v2 and v4 are different runs on different builds, and the pairwise "
        "v4 comparison ignores that the models ran the same issues (conservative).",
        "- The build effect is reported on its own and is not part of the HOLDS call.",
    ]
    ignored = report["inputs"]["ignored_v2_models"]
    if ignored:
        lines.append(
            f"- v2 files also held {len(ignored)} other model(s), not in the study: ignored."
        )
    for side in ("v4", "v2"):
        for f in report["inputs"][side]:
            lines.append(
                f"- Input ({side}): {f['file']}, {f['counted']} of {f['rows']} rows counted, "
                f"sha256 {f['sha256'][:12]}."
            )
    return [*lines, "", LIMITS, ""]


def render(report: Report) -> str:
    """The report as markdown."""
    lines = [
        "# Stage 2 on fixture v4: the pre-registered decision rule",
        "",
        f"**Verdict on the order: {report['verdict']}.**",
        "",
    ]
    for section in (
        models_section,
        effect_section,
        order_section_lines,
        apart_section,
        method_section,
    ):
        lines += section(report)
    return "\n".join(lines).rstrip() + "\n"


def write_atomic(path: Path, text: str) -> None:
    """Writes a file through a temporary name, so a crash never leaves half a report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(path.name + ".tmp")
    staged.write_text(text)
    os.replace(staged, path)


def main(argv: Sequence[str] | None = None) -> int:
    """Builds the report from the files named on the command line; 1 and a message on refusal."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    ap.add_argument("--issues", required=True, help="the seven issue ids, comma separated")
    ap.add_argument("--v4", required=True, type=Path, nargs="+", help="v4 *.scored.jsonl files")
    ap.add_argument("--v2", required=True, type=Path, nargs="+", help="v2 comparator files")
    ap.add_argument("--out", required=True, type=Path, help="the markdown report (JSON beside it)")
    args = ap.parse_args(argv)
    try:
        if args.out.suffix != ".md":
            raise Refusal("--out must end in .md (the JSON goes beside it)")
        report = build_report(parse_ids(args.issues), args.v4, args.v2)
    except Refusal as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    text = render(report)
    write_atomic(args.out, text)
    write_atomic(args.out.with_suffix(".json"), json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
