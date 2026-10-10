"""Cost and throughput of every recorded trial, per model and per task kind.

Depends on: bench.layout (artifact directories, for dates). Run from the repo root:
    python3 spikes/cost_report.py [--md OUT.md] [--json OUT.json]
    python3 spikes/cost_report.py --self-test
Reads `results/**/*.jsonl` except `*.scored.jsonl`, keeps one record per trial_id (a copy outside
`archive/` before one inside it), and joins the `*.scored.jsonl` rows by trial_id for the success
and the task kind: no trial record carries a kind, so a trial with no scored row has none. For each
model and each kind it gives the trials, the wall seconds (mean, median, p90), the startup versus
agent split from `phases`, the tokens, and the successes per GPU-minute with a 95% interval that
resamples issues, the unit the trials cluster in. A field an older record lacks is counted in the
report, never filled in. `--md` replaces the block between the generated-block markers in an
existing file (or creates the file holding just the block), so prose around it survives a rerun.
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import datetime
import json
import random
import re
import statistics
import sys
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench import layout  # noqa: E402

MISSING_ANSWER = ("empty", "tool_json")  # not an answer: counted apart from a wrong one
MIN_ISSUES = 8  # fewer issues in a cell: no interval (the bootstrap is degenerate there)
MIN_P90 = 10  # fewer observations: a p90 is just the maximum
DRAWS = 4000
SEED = 1
NO_KIND = "(no scored row)"
BEGIN = "<!-- BEGIN GENERATED: cost -->"
END = "<!-- END GENERATED: cost -->"
HEADER = """# Cost and throughput of the recorded trials

Per model and per task kind, from every trial record under `results/`. Regenerate with
`python3 spikes/cost_report.py --md results/analysis/cost.md`; `--json OUT.json` also writes every
cell's numbers. The block below is replaced on each run and prose outside it survives.
"""
GPU_FULL = re.compile(r"100%\s*GPU")
CONTEXT_SIZE = re.compile(r"(\d+)\s*$")
COST_FIELDS = ("model", "secs", "phases", "tokens_in", "tokens_out", "steps", "outcome")
Interval = tuple[float, float]


@dataclass
class Census:
    """What was read and what was set aside, so no record disappears unaccounted."""

    files: int = 0
    records: int = 0
    unreadable: int = 0
    no_trial_id: collections.Counter[str] = field(default_factory=collections.Counter)
    overlay_rows: int = 0
    overlay_orphans: int = 0
    copies: collections.Counter[str] = field(default_factory=collections.Counter)
    copy_conflicts: int = 0
    scored_files: int = 0
    scored_rows: int = 0
    scored_copies: int = 0
    scored_orphans: int = 0
    unscorable: int = 0


@dataclass(frozen=True)
class Trial:
    trial_id: str
    model: str
    agent: str
    secs: float
    outcome: str
    steps: int
    tokens_in: int
    tokens_out: int
    phases: dict[str, float]
    day: str | None
    context: int | None
    kind: str  # NO_KIND when there is no scored row
    issue: str
    scored: bool  # a scored row that is not `unscorable`: it counts in the success columns
    success: bool
    answer_kind: str


@dataclass
class Dist:
    n: int
    mean: float
    median: float
    p90: float | None


@dataclass
class Yield:
    """Successes among the scored trials of a cell, against the GPU-minutes those trials took."""

    scored: int
    successes: int
    minutes: float
    per_min: float | None
    not_completed: int
    no_answer: int  # completed failures whose answer_kind is empty or tool_json
    issues: int
    rate_ci: Interval | None
    per_min_ci: Interval | None


@dataclass
class Cell:
    n: int
    completed: int
    not_completed: int
    gpu_hours: float
    first_day: str | None
    last_day: str | None
    contexts: list[int]
    wall: Dist
    wall_completed: Dist | None
    startup: Dist | None
    agent: Dist | None
    startup_share: float | None
    tokens_in: Dist | None
    tokens_out: Dist | None
    zero_step: int
    score: Yield | None


def parse_lines(text: str) -> tuple[list[dict[str, Any]], int]:
    """The JSON objects in `text` and how many non-blank lines were not one (a torn last line)."""
    records: list[dict[str, Any]] = []
    unreadable = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            record = None
        if isinstance(record, dict):
            records.append(record)
        else:
            unreadable += 1
    return records, unreadable


def result_files(results: Path) -> tuple[list[Path], list[Path]]:
    """(trial files, scored files), a file outside `archive/` before one inside it."""
    found = sorted(
        results.rglob("*.jsonl"),
        key=lambda p: ("archive" in p.relative_to(results).parts, p.as_posix()),
    )
    scored = [p for p in found if p.name.endswith(".scored.jsonl")]
    return [p for p in found if p not in scored], scored


def collect(results: Path, census: Census) -> dict[str, dict[str, Any]]:
    """One trial record per trial_id; rows without a trial_id or a `secs` are counted, not used."""
    kept: dict[str, dict[str, Any]] = {}
    overlay: set[str] = set()
    for path in result_files(results)[0]:
        name = path.relative_to(results).as_posix()
        records, unreadable = parse_lines(path.read_text())  # one read: live files grow
        census.files += 1
        census.records += len(records)
        census.unreadable += unreadable
        for rec in records:
            tid = rec.get("trial_id")
            if not tid:
                census.no_trial_id[name] += 1
            elif "secs" not in rec:
                census.overlay_rows += 1
                overlay.add(str(tid))
            elif tid in kept:
                census.copies[name] += 1
                census.copy_conflicts += int(
                    any(kept[tid].get(f) != rec.get(f) for f in COST_FIELDS)
                )
            else:
                kept[tid] = rec
    census.overlay_orphans = len(overlay - kept.keys())
    return kept


def read_scored(results: Path, census: Census) -> dict[str, dict[str, Any]]:
    """The scored row per trial_id; two rows for one trial that disagree on success are an error."""
    rows: dict[str, dict[str, Any]] = {}
    for path in result_files(results)[1]:
        records, unreadable = parse_lines(path.read_text())
        census.scored_files += 1
        census.unreadable += unreadable
        for row in records:
            tid = str(row.get("trial_id", ""))
            if not tid:
                raise ValueError(f"{path}: a scored row without a trial_id")
            census.scored_rows += 1
            if tid not in rows:
                rows[tid] = row
            elif rows[tid].get("success") != row.get("success"):
                raise ValueError(f"trial {tid} is scored twice with different success")
            else:
                census.scored_copies += 1
    return rows


def day_of(record: dict[str, Any]) -> str | None:
    """The day of the trial's artifact directory (no record has a timestamp), if it exists."""
    recorded = record.get("artifact")
    if not recorded:
        return None
    try:
        stamp = layout.artifact_dir(recorded).stat().st_mtime
    except OSError:
        return None
    return datetime.date.fromtimestamp(stamp).isoformat()


def context_of(gpu: str) -> int | None:
    """The context size `ollama ps` showed at the end of the trial, when the field has one."""
    found = CONTEXT_SIZE.search(gpu)
    return int(found.group(1)) if found else None


def text_of(rec: dict[str, Any], key: str) -> str:
    return str(rec.get(key) or "(not recorded)")


def count_of(rec: dict[str, Any], key: str) -> int:
    return int(rec.get(key) or 0)


def verdict(rec: dict[str, Any], row: dict[str, Any] | None) -> dict[str, Any]:
    """The trial's kind, issue and success, from its scored row (none: no kind, never a success)."""
    tid = str(rec["trial_id"])
    if row is None:
        return {"kind": NO_KIND, "issue": tid, "scored": False, "success": False}
    scored = row.get("outcome") != "unscorable"
    return {
        "kind": str(row.get("kind") or "(unscorable, no kind)"),
        "issue": str(row.get("issue") or tid),
        "scored": scored,
        "success": scored and bool(row.get("success")),
    }


def build_trial(rec: dict[str, Any], row: dict[str, Any] | None) -> Trial:
    return Trial(
        trial_id=str(rec["trial_id"]),
        model=text_of(rec, "model"),
        agent=text_of(rec, "agent"),
        secs=float(rec["secs"]),
        outcome=str(rec.get("outcome", "")),
        steps=count_of(rec, "steps"),
        tokens_in=count_of(rec, "tokens_in"),
        tokens_out=count_of(rec, "tokens_out"),
        phases={k: float(v) for k, v in (rec.get("phases") or {}).items()},
        day=day_of(rec),
        context=context_of(str(rec.get("gpu") or "")),
        answer_kind=str((row or {}).get("answer_kind") or rec.get("answer_kind", "")),
        **verdict(rec, row),
    )


def build(results: Path) -> tuple[list[Trial], Census, dict[str, dict[str, Any]]]:
    census = Census()
    raw = collect(results, census)
    rows = read_scored(results, census)
    census.scored_orphans = len(rows.keys() - raw.keys())
    census.unscorable = sum(1 for r in rows.values() if r.get("outcome") == "unscorable")
    trials = [build_trial(rec, rows.get(tid)) for tid, rec in sorted(raw.items())]
    return trials, census, raw


def dist(values: Sequence[float]) -> Dist | None:
    if not values:
        return None
    p90 = None
    if len(values) >= MIN_P90:
        p90 = statistics.quantiles(values, n=10, method="inclusive")[8]
    return Dist(len(values), statistics.fmean(values), statistics.median(values), p90)


def interval(draws: list[float]) -> Interval | None:
    if not draws:
        return None
    draws.sort()
    return draws[int(0.025 * len(draws))], draws[int(0.975 * len(draws)) - 1]


def cluster_bootstrap(
    clusters: Sequence[tuple[int, int, float]],
) -> tuple[Interval | None, Interval | None]:
    """95% intervals of successes/trials and successes/minute, resampling (k, n, minutes) issues."""
    if len(clusters) < MIN_ISSUES:
        return None, None
    rng = random.Random(SEED)
    rates: list[float] = []
    per_min: list[float] = []
    for _ in range(DRAWS):
        pick = [clusters[rng.randrange(len(clusters))] for _ in clusters]
        k, n, minutes = (sum(c[i] for c in pick) for i in range(3))
        if n:
            rates.append(k / n)
        if minutes:
            per_min.append(k / minutes)
    return interval(rates), interval(per_min)


def issue_clusters(scored: Sequence[Trial]) -> list[tuple[int, int, float]]:
    """Per issue: (successes, trials, GPU-minutes), the unit the bootstrap resamples."""
    by_issue: dict[str, list[Trial]] = collections.defaultdict(list)
    for t in scored:
        by_issue[t.issue].append(t)
    return [
        (sum(t.success for t in g), len(g), sum(t.secs for t in g) / 60) for g in by_issue.values()
    ]


def yield_of(trials: Sequence[Trial]) -> Yield | None:
    scored = [t for t in trials if t.scored]
    if not scored:
        return None
    clusters = issue_clusters(scored)
    rate_ci, per_min_ci = cluster_bootstrap(clusters)
    successes = sum(t.success for t in scored)
    minutes = sum(t.secs for t in scored) / 60
    return Yield(
        scored=len(scored),
        successes=successes,
        minutes=minutes,
        per_min=successes / minutes if minutes else None,
        not_completed=sum(1 for t in scored if t.outcome != "completed"),
        no_answer=sum(
            1
            for t in scored
            if t.outcome == "completed" and not t.success and t.answer_kind in MISSING_ANSWER
        ),
        issues=len(clusters),
        rate_ci=rate_ci,
        per_min_ci=per_min_ci,
    )


def phase_stats(trials: Sequence[Trial]) -> tuple[Dist | None, Dist | None, float | None]:
    """Startup and agent seconds and the startup share of wall, over the trials with `phases`."""
    phased = [t for t in trials if t.phases]
    if not phased:
        return None, None, None
    share = sum(t.phases["startup"] for t in phased) / sum(t.secs for t in phased)
    return (
        dist([t.phases["startup"] for t in phased]),
        dist([t.phases["agent"] for t in phased]),
        share,
    )


def token_stats(trials: Sequence[Trial]) -> tuple[Dist | None, Dist | None]:
    """Tokens over the trials that ran a step (with 0 steps, 0 tokens is not a measurement)."""
    stepped = [t for t in trials if t.steps > 0]
    return dist([t.tokens_in for t in stepped]), dist([t.tokens_out for t in stepped])


def day_span(trials: Sequence[Trial]) -> tuple[str | None, str | None]:
    days = sorted(t.day for t in trials if t.day)
    return (days[0], days[-1]) if days else (None, None)


def summarise(trials: Sequence[Trial]) -> Cell:
    """Wall and cost over every trial; the split and tokens over the completed ones."""
    wall = dist([t.secs for t in trials])
    if wall is None:
        raise ValueError("a cell with no trials")
    done = [t for t in trials if t.outcome == "completed"]
    startup, agent, share = phase_stats(done)
    tokens_in, tokens_out = token_stats(done)
    first_day, last_day = day_span(trials)
    return Cell(
        n=len(trials),
        completed=len(done),
        not_completed=len(trials) - len(done),
        gpu_hours=sum(t.secs for t in trials) / 3600,
        first_day=first_day,
        last_day=last_day,
        contexts=sorted({t.context for t in trials if t.context}),
        wall=wall,
        wall_completed=dist([t.secs for t in done]),
        startup=startup,
        agent=agent,
        startup_share=share,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        zero_step=sum(1 for t in trials if t.steps == 0),
        score=yield_of(trials),
    )


def group(trials: Iterable[Trial], *keys: str) -> dict[tuple[str, ...], list[Trial]]:
    out: dict[tuple[str, ...], list[Trial]] = collections.defaultdict(list)
    for t in trials:
        out[tuple(getattr(t, k) for k in keys)].append(t)
    return out


def summarise_by(trials: Iterable[Trial], *keys: str) -> dict[tuple[str, ...], Cell]:
    return {k: summarise(ts) for k, ts in group(trials, *keys).items()}


def secs_fmt(x: float) -> str:
    return f"{x:,.1f}"


def kilo(x: float) -> str:
    return f"{x / 1000:,.1f}k"


def partial(have: int, n: int) -> str:
    return "" if have == n else f" (n={have} of {n})"


def dist_cell(d: Dist | None) -> str:
    if d is None:
        return "none completed"
    p90 = secs_fmt(d.p90) if d.p90 is not None else f"n<{MIN_P90}"
    return f"{secs_fmt(d.mean)} / {secs_fmt(d.median)} / {p90}"


def split_cell(c: Cell) -> str:
    if c.startup is None or c.agent is None:
        return "not recorded"
    return (
        f"{secs_fmt(c.startup.mean)} / {secs_fmt(c.agent.mean)}{partial(c.startup.n, c.completed)}"
    )


def share_cell(c: Cell) -> str:
    return "not recorded" if c.startup_share is None else f"{100 * c.startup_share:.0f}%"


def tokens_cell(c: Cell) -> str:
    if c.tokens_in is None or c.tokens_out is None:
        return "none recorded"
    both = f"{kilo(c.tokens_in.mean)} / {kilo(c.tokens_out.mean)}"
    return both + partial(c.tokens_in.n, c.completed)


def span(lo: str | None, hi: str | None) -> str:
    if lo is None or hi is None:
        return "not recorded"
    return lo if lo == hi else f"{lo} to {hi}"


def ci_text(ci: Interval | None, scale: float, digits: int, unit: str) -> str:
    if ci is None:
        return ""
    if ci[0] == ci[1]:
        return " [no spread]"
    return f" [{scale * ci[0]:.{digits}f}-{scale * ci[1]:.{digits}f}{unit}]"


def success_cell(y: Yield | None) -> str:
    if y is None:
        return "not scored"
    ci = ci_text(y.rate_ci, 100, 0, "%")
    return f"{y.successes}/{y.scored} = {100 * y.successes / y.scored:.0f}%{ci}"


def per_min_cell(y: Yield | None) -> str:
    if y is None or y.per_min is None:
        return "not scored"
    if y.per_min_ci is None:
        return f"{y.per_min:.3f} [no interval: {y.issues} issue{'s' * (y.issues != 1)}]"
    return f"{y.per_min:.3f}{ci_text(y.per_min_ci, 1, 3, '')}"


def per_success_cell(y: Yield | None) -> str:
    if y is None:
        return "not scored"
    return f"{y.minutes / y.successes:.1f}" if y.successes else "no successes"


Column = tuple[str, Callable[[Cell], str]]
TRIALS: Column = ("trials", lambda c: str(c.n))
NOT_COMPLETED: Column = ("not completed", lambda c: str(c.not_completed))
DATES: Column = ("dates", lambda c: span(c.first_day, c.last_day))
CONTEXTS: Column = ("context", lambda c: "/".join(map(str, c.contexts)) or "not recorded")
GPU_HOURS: Column = ("GPU-h", lambda c: f"{c.gpu_hours:.2f}")
TRIALS_PER_HOUR: Column = ("trials / GPU-h", lambda c: f"{c.n / c.gpu_hours:.1f}")
WALL: Column = ("wall s, all trials: mean / median / p90", lambda c: dist_cell(c.wall))
WALL_DONE: Column = (
    "wall s, completed trials: mean / median / p90",
    lambda c: dist_cell(c.wall_completed),
)
SPLIT: Column = ("startup / agent s (mean, completed)", split_cell)
SHARE: Column = ("startup share of wall", share_cell)
TOKENS: Column = ("tokens in / out (mean, completed)", tokens_cell)
ZERO_STEP: Column = ("trials with 0 steps", lambda c: str(c.zero_step))
SCORED: Column = (
    "scored / not completed",
    lambda c: f"{c.score.scored} / {c.score.not_completed}" if c.score else "0",
)
SUCCESS: Column = ("success", lambda c: success_cell(c.score))
NO_ANSWER: Column = (
    "completed failures with no answer",
    lambda c: str(c.score.no_answer) if c.score else "",
)
GPU_MINUTES: Column = ("GPU-min", lambda c: f"{c.score.minutes:.0f}" if c.score else "")
PER_MIN: Column = ("successes / GPU-min", lambda c: per_min_cell(c.score))
PER_SUCCESS: Column = ("GPU-min per success", lambda c: per_success_cell(c.score))


def table(header: Sequence[str], rows: Iterable[Sequence[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    return lines + ["| " + " | ".join(r) + " |" for r in rows] + [""]


def cell_table(
    label: str, columns: Sequence[Column], items: Sequence[tuple[str, Cell]]
) -> list[str]:
    header = [label, *(name for name, _ in columns)]
    return table(header, ([name, *(fn(c) for _, fn in columns)] for name, c in items))


def by_trials(cells: dict[str, Cell]) -> list[tuple[str, Cell]]:
    return [(f"`{m}`", cells[m]) for m in sorted(cells, key=lambda m: (-cells[m].n, m))]


def by_yield(cells: dict[str, Cell]) -> list[tuple[str, Cell]]:
    def key(model: str) -> tuple[int, float, str]:
        y = cells[model].score
        return (0, -(y.per_min or 0.0), model) if y else (1, 0.0, model)

    return [(f"`{m}`", cells[m]) for m in sorted(cells, key=key)]


def model_tables(cells: dict[str, Cell]) -> list[str]:
    items = by_trials(cells)
    out = ["### Per model, all kinds", "", "Wall time and throughput (`secs`).", ""]
    out += cell_table(
        "model",
        [TRIALS, NOT_COMPLETED, DATES, CONTEXTS, GPU_HOURS, TRIALS_PER_HOUR, WALL, WALL_DONE],
        items,
    )
    out += ["Startup versus agent time (`phases`) and tokens (`tokens_in`, `tokens_out`).", ""]
    out += cell_table("model", [SPLIT, SHARE, TOKENS, ZERO_STEP], items)
    out += [
        "Successes per GPU-minute over each model's own task mix: **compare models within a "
        "kind below, not here.**",
        "",
    ]
    out += cell_table(
        "model", [SCORED, SUCCESS, NO_ANSWER, GPU_MINUTES, PER_MIN, PER_SUCCESS], items
    )
    return out


KIND_COLUMNS = [
    TRIALS,
    NOT_COMPLETED,
    WALL,
    WALL_DONE,
    SPLIT,
    TOKENS,
    SUCCESS,
    NO_ANSWER,
    PER_MIN,
    PER_SUCCESS,
]
UNSCORED_NOTE = (
    "No record carries a task kind, so these trials (the realism studies, the commit bake-off "
    "files, the retrieval A/B, experiments not yet scored) have none; the agent profile is what "
    "the records do say."
)


def kind_tables(trials: Sequence[Trial]) -> list[str]:
    by_kind = group(trials, "kind")
    pooled = {k[0]: summarise(ts) for k, ts in by_kind.items()}
    order = sorted(pooled, key=lambda k: (k == NO_KIND, -pooled[k].n, k))
    out = ["### Per task kind, all models pooled", ""]
    out += cell_table("kind", KIND_COLUMNS, [(f"`{k}`", pooled[k]) for k in order])
    out += ["### Per model and task kind", ""]
    for kind in order:
        if kind != NO_KIND:
            cells = {k[0]: c for k, c in summarise_by(by_kind[(kind,)], "model").items()}
            out += [f"#### `{kind}`", ""] + cell_table("model", KIND_COLUMNS, by_yield(cells))
    unscored = summarise_by(by_kind.get((NO_KIND,), []), "model", "agent")
    out += ["### Trials with no scored row, by model and agent", "", UNSCORED_NOTE, ""]
    items = [(f"`{m}` / {a}", unscored[(m, a)]) for m, a in sorted(unscored)]
    items.sort(key=lambda item: -item[1].n)
    return out + cell_table(
        "model / agent", [TRIALS, NOT_COMPLETED, WALL, WALL_DONE, SPLIT, TOKENS], items
    )


def phases_note(recs: list[dict[str, Any]]) -> str:
    schemas = collections.Counter(str(r.get("schema", "absent")) for r in recs if "phases" not in r)
    by_schema = ", ".join(f"{k} {v}" for k, v in sorted(schemas.items()))
    return f"by `schema`: {by_schema}; the startup / agent split uses the trials that have it"


def gpu_note(recs: list[dict[str, Any]], trials: Sequence[Trial]) -> str:
    texts = [str(r["gpu"]) for r in recs if r.get("gpu")]
    full = sum(1 for g in texts if GPU_FULL.search(g))
    no_context = sum(1 for t in trials if t.context is None)
    return (
        f"`100% GPU` recorded in {full}, any CPU share in {len(texts) - full}; "
        f"no context size in {no_context}"
    )


def digest_note(recs: list[dict[str, Any]]) -> str:
    digests: dict[str, set[str]] = collections.defaultdict(set)
    for r in recs:
        if r.get("model_digest"):
            digests[str(r.get("model"))].add(str(r["model_digest"]))
    multi = sorted(m for m, ds in digests.items() if len(ds) > 1)
    return "models with more than one recorded digest: " + (", ".join(multi) or "none")


def missing_rows(raw: dict[str, dict[str, Any]], trials: Sequence[Trial]) -> list[list[str]]:
    recs = list(raw.values())
    n = len(recs)

    def absent(key: str, empty_too: bool = False) -> str:
        gone = sum(1 for r in recs if key not in r or (empty_too and not r[key]))
        return f"{gone} of {n}"

    def count(test: Callable[[Trial], bool]) -> int:
        return sum(1 for t in trials if test(t))

    environments = collections.Counter(str(r.get("environment", "absent")) for r in recs)
    return [
        [
            "`timestamp`",
            f"all {n}",
            f"dates here are the day of the trial's artifact directory; "
            f"{count(lambda t: t.day is None)} trials have no such directory and no date",
        ],
        [
            "`kind`",
            f"all {n}",
            f"from the scored row; {count(lambda t: t.kind == NO_KIND)} trials have none",
        ],
        ["`phases`", absent("phases"), phases_note(recs)],
        [
            "`tokens_in`, `tokens_out`",
            absent("tokens_in"),
            f"{count(lambda t: t.steps == 0)} trials ran 0 steps and carry 0 tokens, and a record "
            "cannot show whether that is none used or none recorded: token means leave them out "
            "and count them",
        ],
        ["`gpu` (absent or empty)", absent("gpu", empty_too=True), gpu_note(recs, trials)],
        [
            "`answer_kind`",
            absent("answer_kind"),
            "a failure without one counts as a wrong answer, not a missing one",
        ],
        [
            "`environment`",
            absent("environment"),
            "seen: " + ", ".join(f"{k} {v}" for k, v in environments.most_common()),
        ],
        [
            "`model_digest` (absent or empty)",
            absent("model_digest", empty_too=True),
            digest_note(recs),
        ],
    ]


def listing(counter: collections.Counter[str]) -> str:
    return ", ".join(f"`{k}` {v}" for k, v in sorted(counter.items())) or "none"


def census_lines(census: Census, trials: Sequence[Trial]) -> list[str]:
    days = sorted(t.day for t in trials if t.day)
    kinded = sum(1 for t in trials if t.kind != NO_KIND)
    failed = [t for t in trials if t.outcome != "completed"]
    instant = [t for t in failed if t.steps == 0]
    instant_secs = sum(t.secs for t in instant) / len(instant) if instant else 0.0
    return [
        f"- Generated {datetime.date.today().isoformat()}. Trial dates run from "
        f"{days[0] if days else 'not recorded'} to {days[-1] if days else 'not recorded'}.",
        f"- {census.files} trial files, {census.records} records, **{len(trials)} trials** after "
        f"keeping one record per trial_id. Copies dropped: {sum(census.copies.values())} "
        f"({listing(census.copies)}), {census.copy_conflicts} of them differing in a cost field.",
        f"- Set aside: {sum(census.no_trial_id.values())} records with no trial_id "
        f"({listing(census.no_trial_id)}); {census.overlay_rows} grading rows with a trial_id and "
        f"no `secs` ({census.overlay_orphans} without a trial record); {census.unreadable} "
        "unreadable lines.",
        f"- {census.scored_files} scored files, {census.scored_rows} rows ({census.scored_copies} "
        f"repeated, {census.scored_orphans} without a trial record, {census.unscorable} "
        f"`unscorable`). {kinded} of the {len(trials)} trials have a scored row and so a kind; "
        f"{len(trials) - kinded} do not.",
        f"- Cost: {sum(t.secs for t in trials) / 3600:.1f} GPU-hours in all. {len(failed)} trials "
        f"({100 * len(failed) / len(trials):.0f}%) did not complete; {len(instant)} of them ran 0 "
        f"steps and took {instant_secs:.1f} s each on average, so they add almost no GPU time but "
        "sit in the success counts as failures.",
    ]


NOTES = """\
- **Wall seconds** are `secs`: the runner's clock from the start of hook seeding to the end of the
  read-back, so overlay set-up before it and scoring after it are not in it. The four `phases`
  (startup, agent, exit wait, after exit) add up to `secs` in every record that has them. *Startup*
  runs to the first event and so includes any model load; *agent* is first to last event. The
  wall columns are given over all trials and over completed trials; the startup / agent split and
  the tokens are over completed trials only, because a trial that failed before its first step
  has a startup and nothing else.
- **GPU-minute** is a wall minute of a trial. This is an assumption: trials run one at a time under
  the harness's session lock, so wall time is the time the GPU was held. It also counts sandbox
  start and read-back, which do not use the GPU, so it overstates GPU time a little. No record has
  a start time, so two overlapping runs could not be detected.
- **Tokens** are sums over steps of opencode's per-step `tokens.input` and `tokens.output`
  (`bench/transcript.py`). Each step re-sends the context, so `tokens in` is not a prompt size, and
  the parser does not keep a cached-token split, so how much of it was cached is not recorded.
- **Success** is the scored row's `success`. `unscorable` rows count in the cost columns but not in
  the success columns. A trial that did not complete (an agent error, a timeout) is a scored failure
  with its GPU-minutes included, because that is what it cost; "not completed" shows how many there
  are, and a capability comparison should set them aside. "Completed failures with no answer" are
  failures of a completed trial whose `answer_kind` is empty or `tool_json`, counted apart from a
  wrong answer; the rest of a cell's failures are wrong answers.
- **Intervals** are 95% percentile bootstraps (4,000 draws, seed 1) over *issues*, resampling an
  issue's (successes, trials, minutes) as one unit, because the repeats of an issue are not
  independent. A cell with fewer than 8 issues gets no interval; a trial-level one would be too
  narrow there. A cell whose issues all agree (every trial a failure, say) has nothing to resample
  and shows `no spread`, which is not a claim of certainty: 0 of 25 trials is, by a trial-level
  Wilson interval, anything up to 13%.
- **p90** is shown from 10 observations; below that it is only the maximum.
- **Pooled, not controlled.** A model's row pools its fixture profiles, rule variants (arms),
  environments and context sizes, and the models ran different task mixes (see the trial counts).
  The per-kind tables control for kind, not for the issue within it.
"""


def render(trials: Sequence[Trial], census: Census, raw: dict[str, dict[str, Any]]) -> str:
    cells = {k[0]: c for k, c in summarise_by(trials, "model").items()}
    out = [BEGIN, "", "## Cost and throughput", "", *census_lines(census, trials), ""]
    out += ["### Fields missing from older records", ""]
    out += table(["field", "absent from", "consequence"], missing_rows(raw, trials))
    out += model_tables(cells) + kind_tables(trials)
    out += ["### Definitions and cautions", "", NOTES + END]
    return "\n".join(out)


def write_block(path: Path, block: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(HEADER + "\n" + block + "\n")
        return
    text = path.read_text()
    if BEGIN in text and END in text:
        head, rest = text.split(BEGIN, 1)
        path.write_text(head + block + rest.split(END, 1)[1])
    else:
        path.write_text(text.rstrip("\n") + "\n\n" + block + "\n")


def payload(trials: Sequence[Trial], census: Census) -> dict[str, Any]:
    def cells(*keys: str) -> list[dict[str, Any]]:
        return [
            {**dict(zip(keys, k, strict=True)), **dataclasses.asdict(c)}
            for k, c in sorted(summarise_by(trials, *keys).items())
        ]

    return {
        "generated": datetime.date.today().isoformat(),
        "census": {k: dict(v) if isinstance(v, dict) else v for k, v in vars(census).items()},
        "trials": len(trials),
        "by_model": cells("model"),
        "by_kind": cells("kind"),
        "by_model_kind": cells("model", "kind"),
        "unscored_by_model_agent": [
            c for c in cells("model", "agent", "kind") if c["kind"] == NO_KIND
        ],
    }


def synthetic(tid: str, secs: float, model: str = "m", **extra: Any) -> dict[str, Any]:
    phases = {"startup": 10.0, "agent": secs - 12.0, "exit_wait": 1.0, "after_exit": 1.0}
    base = {"trial_id": tid, "secs": secs, "model": model, "agent": "coder", "steps": 3}
    base |= {"outcome": "completed", "tokens_in": 1000, "tokens_out": 100, "phases": phases}
    return base | extra


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def scored_row(tid: str, success: bool, **extra: Any) -> dict[str, Any]:
    return {"trial_id": tid, "kind": "k", "issue": "i", "success": success} | extra


Check = tuple[str, bool]


def holds(intervals: tuple[Interval | None, Interval | None], value: float) -> bool:
    """Both intervals (rate, rate per minute) contain `value`."""
    return all(i is not None and i[0] <= value <= i[1] for i in intervals)


def stat_checks() -> list[Check]:
    d = dist([float(i) for i in range(1, 11)])
    few = dist([1.0, 2.0, 3.0])
    records, bad = parse_lines('{"a": 1}\n\n[1]\n{"torn":')
    even = [(1, 2, 2.0)] * MIN_ISSUES
    never = [(0, 3, 3.0)] * MIN_ISSUES
    skew = [(1, 1, 1.0)] * 2 + [(0, 1, 1.0)] * 6
    spread = cluster_bootstrap([(k, 4, 4.0) for k in (0, 1, 2, 3, 4, 1, 3, 2)])[0]
    return [
        ("p90 of 1..10 is 9.1", d is not None and abs((d.p90 or 0) - 9.1) < 1e-9),
        ("p90 is withheld below 10 observations", few is not None and few.p90 is None),
        ("a torn line and a non-object are counted", (len(records), bad) == (1, 2)),
        ("context size is the trailing number", context_of("GB  100% GPU  32768") == 32768),
        ("no trailing number: no context", context_of("100% GPU") is None),
        ("identical issues: no spread, at the ratio", cluster_bootstrap(even) == ((0.5,) * 2,) * 2),
        ("all-failure issues: no spread, at zero", cluster_bootstrap(never) == ((0.0,) * 2,) * 2),
        ("skewed issues: an interval that holds the point", holds(cluster_bootstrap(skew), 0.25)),
        ("below MIN_ISSUES: no interval", cluster_bootstrap(even[:-1]) == (None, None)),
        (
            "spread issues: interval around the pooled rate",
            spread is not None and spread[0] < 0.5 < spread[1],
        ),
    ]


def join_checks(res: Path) -> list[Check]:
    write_jsonl(
        res / "a.jsonl",
        [
            synthetic("t1", 50.0),
            synthetic("t2", 70.0, steps=0, tokens_in=0, tokens_out=0),
            {"note": "no trial id"},
            {"trial_id": "t1", "graded": True},
        ],
    )
    dead = synthetic(
        "t4", 5.0, outcome="agent_error", steps=0, tokens_in=0, tokens_out=0, phases={}
    )
    bare = synthetic("t5", 10.0, steps=0, tokens_in=0, tokens_out=0, phases={})
    write_jsonl(
        res / "archive" / "old.jsonl",
        [synthetic("t1", 51.0), synthetic("t3", 30.0, phases={}), dead, bare],
    )
    scored = [
        scored_row("t1", True),
        scored_row("t2", False, answer_kind="empty"),
        scored_row("t4", False, answer_kind="empty"),
        scored_row("t5", False, outcome="unscorable"),
    ]
    write_jsonl(res / "a.scored.jsonl", [*scored, scored_row("ghost", True)])
    trials, census, raw = build(res)
    by_id = {t.trial_id: t for t in trials}
    cell, y = summarise(trials), yield_of(trials)
    tokens_n = cell.tokens_in.n if cell.tokens_in else None
    block = render(trials, census, raw)
    return [
        ("one record per trial_id, the non-archive copy", by_id["t1"].secs == 50.0),
        (
            "a differing copy is a conflict",
            (sum(census.copies.values()), census.copy_conflicts) == (1, 1),
        ),
        (
            "a grading row and one with no trial_id are set aside",
            (census.overlay_rows, sum(census.no_trial_id.values())) == (1, 1),
        ),
        ("a scored row with no trial record is counted", census.scored_orphans == 1),
        (
            "an unscorable row keeps its kind but is not scored",
            (by_id["t5"].kind, by_id["t5"].scored, census.unscorable) == ("k", False, 1),
        ),
        (
            "a trial with no scored row has no kind",
            by_id["t3"].kind == NO_KIND and not by_id["t3"].scored,
        ),
        ("tokens leave out 0-step trials and count them", (tokens_n, cell.zero_step) == (2, 3)),
        (
            "startup / agent use only trials with phases",
            cell.startup is not None and cell.startup.n == 2,
        ),
        (
            "1 success in 3 scored trials over 125 s",
            y is not None and (y.scored, y.successes, y.minutes) == (3, 1, 125 / 60),
        ),
        (
            "no answer counts a completed failure, not an agent error",
            y is not None and (y.no_answer, y.not_completed) == (1, 1),
        ),
        ("one issue gives no interval", y is not None and y.per_min_ci is None),
        ("the report names the model and the kind", "`m`" in block and "`k`" in block),
    ]


def write_checks(res: Path) -> list[Check]:
    trials, census, raw = build(res)
    target = res / "out.md"
    target.write_text("# Mine\n\nprose\n\n" + BEGIN + "\nOLD-SENTINEL\n" + END + "\n\nafter\n")
    write_block(target, render(trials, census, raw))
    kept = target.read_text()
    write_jsonl(res / "b.scored.jsonl", [scored_row("t1", False)])
    try:
        build(res)
        conflict = False
    except ValueError:
        conflict = True
    return [
        (
            "a rewrite keeps the prose around the block",
            kept.startswith("# Mine\n\nprose\n\n" + BEGIN)
            and kept.endswith(END + "\n\nafter\n")
            and "OLD-SENTINEL" not in kept,
        ),
        ("a trial scored twice with different success is an error", conflict),
    ]


def self_test() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        checks = stat_checks() + join_checks(Path(tmp)) + write_checks(Path(tmp))
    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(("PASS " if ok else "FAIL ") + name)
    print(f"{len(checks) - len(failed)}/{len(checks)} checks passed")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--results", type=Path, default=ROOT / "results", help="the results directory")
    ap.add_argument("--md", type=Path, help="write or refresh the generated block in this file")
    ap.add_argument("--json", type=Path, help="write the per-cell numbers here")
    ap.add_argument("--self-test", action="store_true", help="run the built-in checks and exit")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    trials, census, raw = build(args.results)
    block = render(trials, census, raw)
    if args.md:
        write_block(args.md, block)
    else:
        print(block)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload(trials, census), indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
