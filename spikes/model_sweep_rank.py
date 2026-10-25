"""Rank the per-kind model sweep (plans/MODEL_SWEEP.md, decisions 4, 5 and 11).

One score per model: k, the count of the kind's primary metric over the kind's N issues, taking for
each issue the lowest-numbered repeat with a scorable row (a screened model has three repeats, a
sweep model one; its other repeats are not ranked, only printed as an n = 3 cell). Sorted by k
descending, then the smaller model on disk (GiB to two decimals, from the Ollama API), then the lower
median seconds per trial, then the name. The top 3 go forward only when k is at least ceil(0.75 N);
nothing pads a short list. A study that stopped early (lever 2, decision 11) has no k and no rank: it
prints as "at most N - F". A screened model's rows are used only when their fixture tree, harness tree
and grading hash match the sweep's own rows (decision 4); otherwise it is listed as needing a run.
The kind's own code classifies each trial (`analyse` of spikes/specified_edit_report.py,
`load_verdicts` of spikes/tested_edit_report.py); nothing is re-implemented here.
Depends on: bench.{layout,registry,stats}, spikes/{specified,tested}_edit_report.py.
Run from the repo root once the studies are scored (scored file beside its raw file, or the raw file
under results/experiments/):
    python3 -B spikes/model_sweep_rank.py specified-edit A.scored.jsonl [B.scored.jsonl ...] [--out R.md]
Nothing is cleared, routed or switched by this.
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import importlib.util
import json
import statistics
import sys
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench import layout, registry  # noqa: E402
from bench.stats import wilson  # noqa: E402

KINDS = {
    "specified-edit": {"profile": "specified-edit", "metric": "byte_exact", "report": "specified_edit_report"},
    "tested-edit": {"profile": "tested-edit", "metric": "correct", "report": "tested_edit_report"},
}  # fmt: skip
TAGS_URL = "http://localhost:11434/api/tags"
FINGERPRINT_FIELDS = ("fixture_tree_hash", "harness_tree", "grading_sha256")


class Refusal(Exception):
    """The inputs are not what the plan requires; nothing is reported."""


# ---------------------------------------------------------------- the rules (pure)
def floor_for(n_issues: int) -> int:
    """ceil(0.75 N): the k a top-3 model needs to go forward (6 of 8, 8 of 10, 7 of 9)."""
    return (3 * n_issues + 3) // 4


def max_failures(n_issues: int) -> int:
    """Scorable failures after which a model can no longer reach the floor (decision 11)."""
    return n_issues - floor_for(n_issues) + 1


def normal_name(name: str) -> str:
    """A model name as the rows write it: Ollama's `:latest` suffix is dropped."""
    return name[: -len(":latest")] if name.endswith(":latest") else name


@dataclasses.dataclass(frozen=True)
class Trial:
    """One trial as the kind's own code classified it."""

    issue: str
    repeat: int
    primary: bool  # byte_exact (specified_edit) or correct (tested_edit)
    fault: bool  # an infrastructure fault: never scored
    missing: bool  # a missing answer, kept in n and counted apart
    secs: float
    gpu: str = ""
    secondary: bool = False  # scorer success (specified_edit) or silent_wrong (tested_edit)


@dataclasses.dataclass(frozen=True)
class Study:
    """One model's rows."""

    model: str
    disk_gib: float
    trials: tuple[Trial, ...]
    fingerprint: tuple[str, ...] | None = None  # FINGERPRINT_FIELDS, None when unknown

    @property
    def reused(self) -> bool:
        """A screen's rows (several repeats), not a sweep study (one)."""
        return any(t.repeat > 1 for t in self.trials)


@dataclasses.dataclass(frozen=True)
class Row:
    """One model's place in the table."""

    model: str
    disk_gib: float
    status: str  # complete | stopped | partial
    k: int  # successes among the picked trials
    scored: int  # issues with a scorable trial
    failures: int
    upper: int  # the most k can still be: k + issues without a scorable trial, less the failures
    low: float
    high: float
    median_secs: float
    faults: int
    missing: int
    gpu: str
    secondary: int
    reused: bool
    cell: tuple[int, int] | None  # k and n over every scorable repeat, for a screened model


def pick(trials: Iterable[Trial]) -> dict[str, Trial]:
    """Per issue, the lowest-numbered repeat with a scorable row (decision 4); faults are skipped."""
    chosen: dict[str, Trial] = {}
    for t in trials:
        if t.fault:
            continue
        if t.issue not in chosen or t.repeat < chosen[t.issue].repeat:
            chosen[t.issue] = t
    return chosen


def summarise(study: Study, n_issues: int) -> Row:
    """k and its companions for one model, from the picked trials only."""
    chosen = pick(study.trials)
    k = sum(t.primary for t in chosen.values())
    failures = len(chosen) - k
    if len(chosen) >= n_issues:
        status = "complete"
    elif failures >= max_failures(n_issues):
        status = "stopped"
    else:
        status = "partial"
    low, high = wilson(k, len(chosen))
    gpus = collections.Counter(t.gpu for t in chosen.values() if t.gpu)
    every = [t for t in study.trials if not t.fault]
    return Row(
        model=study.model,
        disk_gib=study.disk_gib,
        status=status,
        k=k,
        scored=len(chosen),
        failures=failures,
        upper=n_issues - failures,
        low=low,
        high=high,
        median_secs=statistics.median(t.secs for t in chosen.values()) if chosen else float("nan"),
        faults=sum(t.fault for t in study.trials),
        missing=sum(t.missing for t in chosen.values()),
        gpu=gpus.most_common(1)[0][0] if gpus else "",
        secondary=sum(t.secondary for t in chosen.values()),
        reused=study.reused,
        cell=(sum(t.primary for t in every), len(every)) if study.reused else None,
    )


def sort_key(row: Row) -> tuple[Any, ...]:
    """Decision 5: k descending, smaller disk size, lower median seconds, then the name."""
    median = row.median_secs if row.median_secs == row.median_secs else float("inf")
    return (-row.k, round(row.disk_gib, 2), median, row.model)


@dataclasses.dataclass(frozen=True)
class Ranking:
    """The table and what follows from it."""

    n_issues: int
    floor: int
    ranked: tuple[Row, ...]  # complete studies, in rank order
    stopped: tuple[Row, ...]
    partial: tuple[Row, ...]
    top3: tuple[Row, ...]
    forward: tuple[Row, ...]  # the top 3 at or above the floor
    dropped: tuple[Row, ...]  # top 3 below the floor
    band: tuple[Row, ...]  # within one trial of the third-ranked k (ranked rows)
    possible: tuple[Row, ...]  # stopped or partial rows that could still be within the band


def rank(studies: Sequence[Study], n_issues: int) -> Ranking:
    """The ranking, the floor, the top 3 and the boundary band."""
    rows = [summarise(s, n_issues) for s in studies]
    ranked = tuple(sorted((r for r in rows if r.status == "complete"), key=sort_key))
    stopped = tuple(sorted((r for r in rows if r.status == "stopped"), key=lambda r: r.model))
    partial = tuple(sorted((r for r in rows if r.status == "partial"), key=lambda r: r.model))
    floor = floor_for(n_issues)
    top3 = ranked[:3]
    forward = tuple(r for r in top3 if r.k >= floor)
    dropped = tuple(r for r in top3 if r.k < floor)
    band: tuple[Row, ...] = ()
    possible: tuple[Row, ...] = ()
    if len(ranked) >= 3:
        k3 = ranked[2].k
        band = tuple(r for r in ranked if abs(r.k - k3) <= 1)
        possible = tuple(r for r in (*stopped, *partial) if r.upper >= k3 - 1)
    return Ranking(
        n_issues, floor, ranked, stopped, partial, top3, forward, dropped, band, possible
    )


def fingerprint_mismatches(
    studies: Sequence[Study],
) -> dict[str, list[str]]:
    """For each screened study, the fingerprint fields that differ from the sweep studies' (decision 4).

    The reference is the fingerprint all sweep studies share. Without sweep studies, or with sweep
    studies that disagree, or a screened study of unknown fingerprint, nothing can be verified and the
    study is listed with that reason.
    """
    sweep = {s.fingerprint for s in studies if not s.reused and s.fingerprint is not None}
    bad: dict[str, list[str]] = {}
    for s in studies:
        if not s.reused:
            continue
        if len(sweep) != 1:
            bad[s.model] = ["no single fingerprint among the sweep studies to compare with"]
        elif s.fingerprint is None:
            bad[s.model] = ["its fingerprint is unknown"]
        else:
            (ref,) = sweep
            diff = [
                f for f, a, b in zip(FINGERPRINT_FIELDS, s.fingerprint, ref, strict=True) if a != b
            ]
            if diff:
                bad[s.model] = diff
    return bad


def usable(studies: Sequence[Study]) -> tuple[list[Study], dict[str, list[str]]]:
    """The studies whose rows may be ranked, and the screened ones whose fingerprint differs."""
    bad = fingerprint_mismatches(studies)
    return [s for s in studies if s.model not in bad], bad


# ---------------------------------------------------------------- output (pure)
def _line(place: str, r: Row, n_issues: int) -> str:
    """One table row; a stopped or unfinished study has no k and no interval."""
    note = []
    if r.reused and r.cell:
        note.append(f"screen, n = 3 cell {r.cell[0]}/{r.cell[1]}")
    if r.status == "stopped":
        note.append(
            f"stopped after {r.failures} failures in {r.scored} trials, below the floor; at most {r.upper}"
        )
    elif r.status == "partial":
        note.append(f"unfinished: {r.scored} of {n_issues} issues scored, at most {r.upper}")
    done = r.status == "complete"
    k_cell = f"{r.k}/{n_issues}" if done else "-"
    interval = f"[{r.low:.2f}, {r.high:.2f}]" if done else "-"
    median = f"{r.median_secs:.0f}" if r.median_secs == r.median_secs else "-"
    return (
        f"| {place} | {r.model} | {k_cell} | {interval} | {median} "
        f"| {r.disk_gib:.2f} | {r.faults} | {r.missing} | {r.gpu or '-'} | {r.secondary} | {'; '.join(note)} |"
    )  # fmt: skip


def markdown(
    kind: str, result: Ranking, rejected: Mapping[str, Sequence[str]] | None = None
) -> str:
    """The ranked table and everything the plan wants printed beside it."""
    spec = KINDS[kind]
    metric2 = "scorer success" if kind == "specified-edit" else "silent_wrong"
    n = result.n_issues
    out = [
        f"# Model sweep: {kind} (plans/MODEL_SWEEP.md)",
        "",
        f"Primary metric `{spec['metric']}`, k of N = {n} issues, one scorable trial per issue (n = 1; a screened "
        "model's lowest-numbered repeat). Ties go to the smaller model on disk, then the lower median seconds, "
        "then the name. Wilson 95% intervals. Nothing is cleared, routed or switched to default from this.",
        "",
        f"| rank | model | k/N | Wilson | median s | disk GiB | infra faults | missing | gpu | {metric2} | note |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    out += [_line(str(i), r, n) for i, r in enumerate(result.ranked, 1)]
    out += [_line("-", r, n) for r in (*result.stopped, *result.partial)]
    out += [
        "",
        f"**Floor:** k >= ceil(0.75 N) = {result.floor} of {n}.",
        f"**Top 3:** {', '.join(r.model for r in result.top3) or 'none'}.",
        f"**Go forward** (top 3 at or above the floor): {', '.join(r.model for r in result.forward) or 'none'}"
        + (f" ({len(result.forward)} of 3; nothing pads the list)." if len(result.forward) < 3 else "."),
    ]  # fmt: skip
    if result.dropped:
        out.append(
            "**Dropped by the floor:** "
            + ", ".join(f"{r.model} (k = {r.k})" for r in result.dropped)
            + "."
        )
    if result.band:
        k3 = result.ranked[2].k
        out.append(
            f"**Boundary band** (within one trial of the third-ranked k = {k3}; one trial moves a rate by "
            f"{1 / n:.3f}): " + ", ".join(f"{r.model} ({r.k})" for r in result.band) + "."
        )
        if result.possible:
            out.append(
                "Stopped or unfinished models that could still be in the band: "
                + ", ".join(f"{r.model} (at most {r.upper})" for r in result.possible)
                + "."
            )
    else:
        out.append("**Boundary band:** none (fewer than three models have a complete study).")
    if rejected:
        out += [
            "",
            "**Screen rows not used (decision 4: fingerprint differs; run these models in the sweep):**",
        ]
        out += [f"- {m}: {', '.join(why)}" for m, why in sorted(rejected.items())]  # fmt: skip
    every = (*result.ranked, *result.stopped, *result.partial)
    out += [
        "",
        f"Infrastructure faults (not scored): {sum(r.faults for r in every)}. "
        f"Missing answers (kept in n): {sum(r.missing for r in result.ranked)} in complete studies.",
    ]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- the impure shell
def _spike(name: str) -> ModuleType:
    """A sibling spike script as a module."""
    spec = importlib.util.spec_from_file_location(f"spike_{name}", ROOT / "spikes" / f"{name}.py")
    assert spec and spec.loader
    module = sys.modules.get(spec.name) or importlib.util.module_from_spec(spec)
    if spec.name not in sys.modules:
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return module


def read_sizes(
    path: Path | None = None, fetch: Callable[[str], bytes] | None = None
) -> dict[str, float]:
    """Disk GiB per model name from an `/api/tags` document (a file, or the live API; both read only)."""
    if path is not None:
        doc = json.loads(path.read_text())
    else:
        get = fetch or (lambda url: urllib.request.urlopen(url, timeout=10).read())  # noqa: S310
        doc = json.loads(get(TAGS_URL))
    return {normal_name(m["name"]): m["size"] / 2**30 for m in doc["models"]}


def find_raw(scored: Path, trial_id: str, search: Path) -> Path:
    """The raw file of a scored file: its sibling, else the file under `search` holding the trial."""
    sibling = scored.with_name(scored.name.replace(".scored.jsonl", ".jsonl"))
    if sibling != scored and sibling.exists():
        return sibling
    for candidate in sorted(search.glob("*.jsonl")):
        if not candidate.name.endswith(".scored.jsonl") and trial_id in candidate.read_text():
            return candidate
    raise Refusal(f"{scored}: no raw file holds trial {trial_id}")


def grading_hash(experiment_id: str, rows: Sequence[Mapping[str, Any]]) -> str:
    """The grading hash of an experiment's last registration ('' when it was not registered)."""
    found = [
        r.get("grading_sha256", "")
        for r in rows
        if r.get("event") == "registered" and r.get("id") == experiment_id
    ]
    return str(found[-1]) if found else ""  # fmt: skip


def _fingerprint(
    records: Sequence[Mapping[str, Any]], reg: Sequence[Mapping[str, Any]]
) -> tuple[str, ...] | None:
    """(fixture tree hash, harness tree, grading hash) when every record agrees, else None."""
    seen = {
        (str(r.get("fixture_tree_hash", "")), str(r.get("harness_tree", "")), grading_hash(str(r.get("experiment_id", "")), reg))
        for r in records
    }  # fmt: skip
    return next(iter(seen)) if len(seen) == 1 else None


def specified_trials(
    raw: Sequence[dict[str, Any]], scored: Sequence[dict[str, Any]], report: ModuleType
) -> list[Trial]:
    """Trials for specified_edit: byte_exact from the report's own `analyse`, one row at a time."""
    records = {r["trial_id"]: r for r in raw}
    profiles = layout.version_dir(layout.VERSION) / "profiles"
    out = []
    for row in scored:
        rec = records[row["trial_id"]]
        base: dict[str, Any] = dict(
            issue=row["issue"], repeat=int(row.get("repeat") or 1), secs=float(row.get("secs") or 0.0),
            gpu=str(rec.get("gpu", "")),
        )  # fmt: skip
        if report.infra_fault(row, rec.get("steps")):
            out.append(Trial(**base, primary=False, fault=True, missing=False))
            continue
        a = report.analyse([rec], [row], profiles, ROOT / "issues")
        out.append(
            Trial(**base, primary=a["byte_exact"]["k"] == 1, fault=False,
                  missing=bool(a["missing_answers"]), secondary=bool(row.get("success")))
        )  # fmt: skip
    return out


def tested_trials(
    scored_path: Path, report: ModuleType, with_named: bool = True
) -> tuple[str, list[Trial]]:
    """Trials for tested_edit from the report's own `load_verdicts` (the named test is a CPU run per trial)."""
    saved = report.__dict__["named_pass_for"]
    if not with_named:
        report.__dict__["named_pass_for"] = lambda *a, **k: None
    try:
        model, verdicts = report.load_verdicts(scored_path, 1)
    finally:
        report.__dict__["named_pass_for"] = saved
    rows = {r["trial_id"]: r for r in map(json.loads, scored_path.read_text().splitlines())}
    raw_path = scored_path.with_name(scored_path.name.replace(".scored.jsonl", ".jsonl"))
    gpus = {
        r["trial_id"]: str(r.get("gpu", ""))
        for r in map(json.loads, raw_path.read_text().splitlines())
        if "trial_id" in r
    }
    return model, [
        Trial(
            issue=v.issue, repeat=int(rows[v.trial_id].get("repeat") or 1), primary=v.correct,
            fault=bool(v.fault), missing=v.missing_answer, secs=v.secs, gpu=gpus.get(v.trial_id, ""),
            secondary=v.silent_wrong,
        )
        for v in verdicts
    ]  # fmt: skip


def load_study(
    kind: str, scored_path: Path, sizes: Mapping[str, float], with_named: bool = True,
    search: Path | None = None, reg: Sequence[Mapping[str, Any]] = (),
) -> Study:  # fmt: skip
    """One model's Study from its scored file (and the raw file beside it)."""
    scored = [json.loads(line) for line in scored_path.read_text().splitlines() if line.strip()]
    if not scored:
        raise Refusal(f"{scored_path}: no rows")
    model = normal_name(str(scored[0]["model"]))
    if any(normal_name(str(r["model"])) != model for r in scored):
        raise Refusal(f"{scored_path}: more than one model in one file")
    if model not in sizes:
        raise Refusal(f"{model}: not in the Ollama tag list (no disk size)")
    raw_path = find_raw(
        scored_path, scored[0]["trial_id"], search or layout.RESULTS / "experiments"
    )
    raw = [json.loads(line) for line in raw_path.read_text().splitlines() if line.strip()]
    ids = {r["trial_id"] for r in scored}
    report = _spike(str(KINDS[kind]["report"]))
    if kind == "specified-edit":
        trials = specified_trials(raw, scored, report)
    else:
        if raw_path.with_name(raw_path.name.replace(".jsonl", ".scored.jsonl")) != scored_path:
            raise Refusal(f"{scored_path}: tested_edit needs the scored file beside its raw file")
        _, trials = tested_trials(scored_path, report, with_named)
    mine = [r for r in raw if r.get("trial_id") in ids]
    return Study(model, sizes[model], tuple(trials), _fingerprint(mine, reg))


def main(argv: Sequence[str] | None = None) -> int:
    """Print the ranking for the given scored files and write it to `--out`."""
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("kind", choices=sorted(KINDS) + ["specified_edit", "tested_edit"])
    ap.add_argument("scored", nargs="+", type=Path, help="one scored file per model")
    ap.add_argument("--sizes", type=Path, help="a saved /api/tags document (default: the live API)")
    ap.add_argument("--n-issues", type=int, help="N (default: the issues of the kind's profile)")
    ap.add_argument("--out", type=Path)
    ap.add_argument(
        "--skip-named", action="store_true", help="tested_edit: skip the named-test runs"
    )
    args = ap.parse_args(argv)
    kind = args.kind.replace("_", "-")
    try:
        from bench.issues import schema

        n = args.n_issues or len(
            schema.load_profile(ROOT / "issues" / "profiles" / f"{KINDS[kind]['profile']}.toml")[1]
        )
        sizes = read_sizes(args.sizes)
        reg = registry.read(layout.ROOT)
        studies = [load_study(kind, p, sizes, not args.skip_named, reg=reg) for p in args.scored]
        names = collections.Counter(s.model for s in studies)
        if dup := [m for m, c in names.items() if c > 1]:
            raise Refusal(f"{dup}: more than one file for the same model")
        keep, rejected = usable(studies)
        text = markdown(kind, rank(keep, n), rejected)
    except Refusal as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(text)
    if args.out:
        args.out.write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
