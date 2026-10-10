"""Test-retest reliability and a multi-model difficulty rating from repeated, scored trials.

Depends on: bench.stats, bench.jsonl. Run from the repo root:
    python3 spikes/reliability.py A.scored.jsonl B.scored.jsonl [--md OUT.md] [--json OUT.json]
    python3 spikes/reliability.py --self-test
Each input is one model's scored file, every issue run the same number of times (the repeats). An
issue's repeats are the "raters" of a binary success outcome, so the report gives, per model, per
task kind and per expected-answer type: pairwise agreement, Fleiss' kappa, the one-way intraclass
correlation ICC(1,1) (the share of outcome variance that belongs to the issue, not to the draw), the
reliability of an issue's mean over its repeats ICC(1,k), and the share of issues whose repeats all
agree. Intervals are percentile bootstraps over issues (issues are the unit that is sampled, the
repeats are not independent draws of anything else), and a group with fewer than MIN_ISSUES issues
gets no ICC at all. Across models it gives the rank correlation of per-issue success and a
difficulty rating per issue (definition at `rating`), then lists the issues no model solved, with
their trial ids so the transcripts can be read. Rows that are not valid runs (not completed, or
`unscorable`) are dropped and counted. `--md` replaces the block between the generated-block
markers in an existing file (or creates the file holding just the block), so prose around it
survives a rerun.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import random
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench.jsonl import read_jsonl  # noqa: E402
from bench.stats import cluster_ci, wilson  # noqa: E402

MISSING = ("empty", "tool_json")  # not an answer: counted apart from a wrong one
MIN_ISSUES = 8  # fewer issues: no interval, kappa or ICC (the bootstrap is degenerate there)
DRAWS = 10_000
SEED = 1
BEGIN = "<!-- BEGIN GENERATED: reliability -->"
END = "<!-- END GENERATED: reliability -->"
NAN = float("nan")
Groups = list[tuple[int, int]]  # per issue: (successes, trials)


@dataclass
class Est:
    """A point estimate with a percentile-bootstrap interval; `lost` draws were undefined."""

    point: float
    low: float
    high: float
    lost: int = 0

    def fmt(self, digits: int = 2) -> str:
        if math.isnan(self.point):
            return "n/a"
        if math.isnan(self.low):
            return f"{self.point:.{digits}f} [n/a]"
        return f"{self.point:.{digits}f} [{self.low:.{digits}f}, {self.high:.{digits}f}]"


@dataclass
class ModelRuns:
    """One model's valid trials, per issue in repeat order."""

    name: str
    wins: dict[str, list[bool]] = field(default_factory=dict)
    labels: dict[str, list[str]] = field(default_factory=dict)
    trial_ids: dict[str, list[str]] = field(default_factory=dict)
    kind: dict[str, str] = field(default_factory=dict)
    expected: dict[str, str] = field(default_factory=dict)
    missing: dict[str, int] = field(default_factory=dict)
    dropped: int = 0

    def groups(self, issues: Sequence[str]) -> Groups:
        return [(sum(self.wins[i]), len(self.wins[i])) for i in issues if i in self.wins]


def load_runs(path: Path) -> ModelRuns:
    """A scored file as one model's runs; the last row per (issue, repeat) wins."""
    rows = read_jsonl(path)
    names = collections.Counter(str(r.get("model", "")) for r in rows)
    runs = ModelRuns(name=names.most_common(1)[0][0] if names else path.stem)
    cells: dict[tuple[str, int], dict[str, Any]] = {}
    for row in rows:
        if (
            row.get("outcome") == "unscorable"
            or row.get("trial_outcome", "completed") != "completed"
        ):
            runs.dropped += 1
            continue
        cells[(str(row["issue"]), int(row.get("repeat", 0)))] = row
    for (issue, _), row in sorted(cells.items()):
        runs.wins.setdefault(issue, []).append(bool(row.get("success")))
        runs.labels.setdefault(issue, []).append(str(row.get("outcome", "")))
        runs.trial_ids.setdefault(issue, []).append(str(row.get("trial_id", "")))
        runs.kind[issue] = str(row.get("kind", "?"))
        runs.expected[issue] = str(row.get("expected", "?"))
        runs.missing[issue] = runs.missing.get(issue, 0) + (row.get("answer_kind") in MISSING)
    return runs


# --- agreement statistics -------------------------------------------------------------------


def pair_agreement(c: int, n: int) -> float:
    """Share of the n(n-1)/2 repeat pairs that agree, for c successes of n."""
    if n < 2:
        return NAN
    return (c * (c - 1) + (n - c) * (n - c - 1)) / (n * (n - 1))


def observed_agreement(groups: Groups) -> float:
    """Mean over issues of the pairwise agreement of their repeats."""
    vals = [pair_agreement(c, n) for c, n in groups if n >= 2]
    return sum(vals) / len(vals) if vals else NAN


def fleiss_kappa(groups: Groups) -> float:
    """Fleiss' kappa for a binary outcome: agreement beyond what the pooled rate predicts."""
    total = sum(n for _, n in groups)
    p = sum(c for c, _ in groups) / total if total else NAN
    p_e = p * p + (1 - p) ** 2
    p_o = observed_agreement(groups)
    if math.isnan(p_o) or math.isnan(p_e) or p_e >= 1.0:
        return NAN  # every trial had the same result: there is nothing to agree beyond
    return (p_o - p_e) / (1 - p_e)


def _anova(groups: Groups) -> tuple[float, float, float] | None:
    """(MSB, MSW, n0) of the one-way layout for binary counts; None when it is undefined."""
    groups = [(c, n) for c, n in groups if n >= 1]
    k, total = len(groups), sum(n for _, n in groups)
    if k < 2 or total <= k:
        return None
    grand = sum(c for c, _ in groups) / total
    ssb = sum(n * (c / n - grand) ** 2 for c, n in groups)
    ssw = sum(c * (n - c) / n for c, n in groups)
    n0 = (total - sum(n * n for _, n in groups) / total) / (k - 1)
    return ssb / (k - 1), ssw / (total - k), n0


def icc1(groups: Groups) -> float:
    """ICC(1,1), one-way random effects: the share of outcome variance that is between issues."""
    anova = _anova(groups)
    if anova is None:
        return NAN
    msb, msw, n0 = anova
    denom = msb + (n0 - 1) * msw
    return (msb - msw) / denom if denom > 0 else NAN


def icc_k(groups: Groups) -> float:
    """ICC(1,k): reliability of an issue's mean over its repeats (the Spearman-Brown step-up)."""
    anova = _anova(groups)
    if anova is None:
        return NAN
    msb, msw, _ = anova
    return (msb - msw) / msb if msb > 0 else NAN


def icc1_values(rows: Sequence[Sequence[float]]) -> tuple[float, float]:
    """ICC(1,1) and ICC(1,k) from raw ratings, equal rater counts: the textbook form, for checks."""
    n, k = len(rows), len(rows[0])
    grand = sum(sum(r) for r in rows) / (n * k)
    msb = k * sum((sum(r) / k - grand) ** 2 for r in rows) / (n - 1)
    msw = sum((v - sum(r) / k) ** 2 for r in rows for v in r) / (n * (k - 1))
    return (msb - msw) / (msb + (k - 1) * msw), (msb - msw) / msb


def design_effect(icc: float, repeats: float) -> float:
    """Variance inflation of a pooled trial rate from the repeats sharing an issue."""
    return 1 + (repeats - 1) * icc


def _ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for t in range(i, j + 1):
            ranks[order[t]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(pairs: Sequence[tuple[float, float]]) -> float:
    """Rank correlation (average ranks for ties); NaN when either side is constant."""
    if len(pairs) < 3:
        return NAN
    ra, rb = _ranks([a for a, _ in pairs]), _ranks([b for _, b in pairs])
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    va = sum((x - ma) ** 2 for x in ra)
    vb = sum((y - mb) ** 2 for y in rb)
    if va == 0 or vb == 0:
        return NAN
    return sum((x - ma) * (y - mb) for x, y in zip(ra, rb, strict=True)) / math.sqrt(va * vb)


def bootstrap[T](units: Sequence[T], stat: Callable[[list[T]], float]) -> Est:
    """The statistic on `units` with a 95% percentile interval over resamples of the units."""
    units = list(units)
    point = stat(units)
    if len(units) < MIN_ISSUES or math.isnan(point):
        return Est(point, NAN, NAN)
    rng = random.Random(SEED)
    vals = []
    for _ in range(DRAWS):
        v = stat([units[rng.randrange(len(units))] for _ in units])
        if not math.isnan(v):
            vals.append(v)
    lost = DRAWS - len(vals)
    if len(vals) < DRAWS * 0.9:
        return Est(point, NAN, NAN, lost)  # too many resamples had no variance to estimate from
    vals.sort()
    return Est(point, vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals)) - 1], lost)


# --- rating ---------------------------------------------------------------------------------


def rating(k: int, n: int) -> str:
    """Difficulty from the pooled success of all models: unsolved 0; hard up to 1/3; moderate
    below 0.8; easy from 0.8 up. With two models and three repeats each: 0, 1-2, 3-4, 5-6 of 6."""
    if n == 0:
        return "n/a"
    if k == 0:
        return "unsolved"
    p = k / n
    if p <= 1 / 3 + 1e-9:
        return "hard"
    return "moderate" if p < 0.8 else "easy"


def pattern(runs: ModelRuns, issue: str) -> str:
    """Per-repeat results as 1/0, or '-' where the model has no valid trial of the issue."""
    return "".join("1" if w else "0" for w in runs.wins[issue]) if issue in runs.wins else "-"


# --- report ---------------------------------------------------------------------------------


def rate(k: int, n: int) -> str:
    lo, hi = wilson(k, n)
    return f"{k}/{n} [{lo:.2f}, {hi:.2f}]"


def reliability_row(label: str, groups: Groups) -> str:
    """One table row: agreement, kappa, ICCs and unanimity for one group of issues."""
    n_issues = len(groups)
    trials = sum(n for _, n in groups)
    unanimous = sum(1 for c, n in groups if c in (0, n))
    mean_n = trials / n_issues if n_issues else NAN
    est = {
        "po": bootstrap(groups, observed_agreement),
        "kappa": bootstrap(groups, fleiss_kappa),
        "icc1": bootstrap(groups, icc1),
        "icck": bootstrap(groups, icc_k),
    }
    for key in ("kappa", "icc1", "icck"):  # not estimable from a handful of issues
        if n_issues < MIN_ISSUES:
            est[key] = Est(NAN, NAN, NAN)
    deff = design_effect(est["icc1"].point, mean_n) if not math.isnan(est["icc1"].point) else NAN
    return (
        f"| {label} | {n_issues} | {trials} | {est['po'].fmt()} | {est['kappa'].fmt()} | "
        f"{est['icc1'].fmt()} | {est['icck'].fmt()} | {rate(unanimous, n_issues)} | "
        f"{'n/a' if math.isnan(deff) else f'{deff:.2f}'} |"
    )


HEADER = (
    "| group | issues | trials | pairwise agreement | Fleiss kappa | ICC(1,1) | ICC(1,k) | "
    "issues with all repeats equal | design effect |\n|---|---|---|---|---|---|---|---|---|"
)


def by_group(runs: ModelRuns, key: dict[str, str]) -> list[str]:
    """Rows per value of `key` (issue -> group label), largest group first."""
    members: dict[str, list[str]] = collections.defaultdict(list)
    for issue in runs.wins:
        members[key[issue]].append(issue)
    order = sorted(members, key=lambda g: (-len(members[g]), g))
    return [reliability_row(g, runs.groups(members[g])) for g in order]


def model_section(runs: ModelRuns) -> list[str]:
    issues = sorted(runs.wins)
    ok = sum(sum(w) for w in runs.wins.values())
    trials = sum(len(w) for w in runs.wins.values())
    clustered = cluster_ci({i: runs.wins[i] for i in issues}, seed=SEED)
    nonanswers = sum(runs.missing.values())
    out = [
        f"### {runs.name}",
        "",
        f"{len(issues)} issues, {trials} valid trials ({runs.dropped} rows dropped as not valid "
        f"runs). Success {rate(ok, trials)} (Wilson over trials); issue-clustered mean "
        f"{clustered[0]:.2f} [{clustered[1]:.2f}, {clustered[2]:.2f}]. No final answer "
        f"(`empty` or `tool_json`): {nonanswers}/{trials}, counted as failures here.",
        "",
        HEADER,
        reliability_row("all issues", runs.groups(issues)),
        "",
        "By task kind:",
        "",
        HEADER,
        *by_group(runs, runs.kind),
        "",
        "By expected answer type:",
        "",
        HEADER,
        *by_group(runs, runs.expected),
        "",
    ]
    return out


def cross_section(models: list[ModelRuns]) -> list[str]:
    """Rank agreement between models and the difficulty table over the shared issues."""
    shared = sorted(set.intersection(*(set(m.wins) for m in models)))
    out = [f"{len(shared)} issues are scored for every model.", ""]
    for i, a in enumerate(models):
        for b in models[i + 1 :]:
            units = [
                (sum(a.wins[t]) / len(a.wins[t]), sum(b.wins[t]) / len(b.wins[t])) for t in shared
            ]
            est = bootstrap(units, spearman)
            zero_a = {t for t in shared if not any(a.wins[t])}
            zero_b = {t for t in shared if not any(b.wins[t])}
            out += [
                f"- {a.name} vs {b.name}: Spearman rho of per-issue success "
                f"{est.fmt()} (n = {len(shared)} issues, {est.lost} undefined resamples). "
                f"Never solved by {a.name}: {len(zero_a)}; by {b.name}: {len(zero_b)}; by both: "
                f"{len(zero_a & zero_b)}; by {a.name} only: {len(zero_a - zero_b)}; by {b.name} "
                f"only: {len(zero_b - zero_a)}.",
            ]
    out.append("")
    return out


def difficulty_rows(models: list[ModelRuns]) -> list[dict[str, Any]]:
    """One record per issue: per-model patterns, pooled success, rating and the split tag."""
    issues = sorted(set().union(*(set(m.wins) for m in models)))
    records = []
    for issue in issues:
        have = [m for m in models if issue in m.wins]
        k = sum(sum(m.wins[issue]) for m in have)
        n = sum(len(m.wins[issue]) for m in have)
        rates = [sum(m.wins[issue]) / len(m.wins[issue]) for m in have]
        records.append(
            {
                "issue": issue,
                "kind": next(m.kind[issue] for m in have),
                "expected": next(m.expected[issue] for m in have),
                "patterns": {m.name: pattern(m, issue) for m in models},
                "k": k,
                "n": n,
                "wilson": wilson(k, n),
                "rating": rating(k, n) if len(have) == len(models) else "incomplete",
                "split": len(have) > 1 and max(rates) - min(rates) >= 2 / 3 - 1e-9,
                "labels": {m.name: m.labels[issue] for m in have},
                "missing": {m.name: m.missing[issue] for m in have},
                "trial_ids": {m.name: m.trial_ids[issue] for m in have},
            }
        )
    return records


def difficulty_section(models: list[ModelRuns], records: list[dict[str, Any]]) -> list[str]:
    names = [m.name for m in models]
    counts = collections.Counter(r["rating"] for r in records)
    order = ["unsolved", "hard", "moderate", "easy", "incomplete"]
    out = [
        "Ratings: "
        + ", ".join(f"{counts[o]} {o}" for o in order if counts[o])
        + f" ({len(records)} issues). `split` marks issues where the models' success differs "
        "by at least two repeats of three.",
        "",
        "| issue | kind | " + " | ".join(names) + " | pooled k/n [Wilson] | rating | split |",
        "|---|---|" + "---|" * len(names) + "---|---|---|",
    ]
    rank = {o: i for i, o in enumerate(order)}
    for r in sorted(
        records, key=lambda r: (rank[r["rating"]], r["k"] / max(r["n"], 1), r["issue"])
    ):
        lo, hi = r["wilson"]
        out.append(
            f"| {r['issue']} | {r['kind']} | "
            + " | ".join(r["patterns"][n] for n in names)
            + f" | {r['k']}/{r['n']} [{lo:.2f}, {hi:.2f}] | {r['rating']} | "
            + ("yes" if r["split"] else "")
            + " |"
        )
    out.append("")
    return out


def unsolved_section(models: list[ModelRuns], records: list[dict[str, Any]]) -> list[str]:
    zero = [r for r in records if r["k"] == 0 and r["rating"] == "unsolved"]
    out = [f"{len(zero)} issues were solved in none of their trials by any model.", ""]
    if not zero:
        return out
    out += [
        "| issue | kind | trial outcomes per model | no-answer trials | trial ids |",
        "|---|---|---|---|---|",
    ]
    for r in zero:
        outcomes = "; ".join(f"{n}: {' '.join(r['labels'][n])}" for n in r["labels"])
        absent = "; ".join(f"{n}: {r['missing'][n]}" for n in r["missing"])
        ids = "; ".join(f"{n}: {' '.join(r['trial_ids'][n])}" for n in r["trial_ids"])
        out.append(f"| {r['issue']} | {r['kind']} | {outcomes} | {absent} | {ids} |")
    out.append("")
    return out


def build(models: list[ModelRuns]) -> tuple[str, dict[str, Any]]:
    """The generated markdown block and the same numbers as a dict for the JSON file."""
    records = difficulty_rows(models)
    lines = [
        BEGIN,
        "",
        f"_Intervals: 95% percentile bootstrap over issues, {DRAWS} draws, seed {SEED}; "
        f"Wilson where a rate is a count of trials or issues. A group of fewer than "
        f"{MIN_ISSUES} issues gets no kappa, ICC or bootstrap._",
        "",
        "## Test-retest, per model",
        "",
    ]
    for runs in models:
        lines += model_section(runs)
    lines += ["## Across models", "", *cross_section(models)]
    lines += ["## Difficulty rating per issue", "", *difficulty_section(models, records)]
    lines += ["## Issues no model solved", "", *unsolved_section(models, records), END]
    payload = {
        "models": [m.name for m in models],
        "draws": DRAWS,
        "seed": SEED,
        "issues": records,
    }
    return "\n".join(lines) + "\n", payload


def write_block(path: Path, block: str) -> None:
    """Replace the generated block in `path`, or create it holding only the block."""
    if path.exists():
        text = path.read_text()
        if BEGIN in text and END in text:
            head, rest = text.split(BEGIN, 1)
            tail = rest.split(END, 1)[1]
            path.write_text(head + block.rstrip("\n") + tail)
            return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(block)


# --- self-test ------------------------------------------------------------------------------


def self_test() -> int:
    """Known values: a published ICC example, hand-computed kappa cases, ties, and agreement."""
    checks: list[tuple[str, bool]] = []
    # Shrout and Fleiss (1979), table 2: six targets rated by four judges.
    table = [[9, 2, 5, 8], [6, 1, 3, 2], [8, 4, 6, 8], [7, 1, 2, 6], [10, 5, 6, 9], [6, 2, 4, 7]]
    one, many = icc1_values([[float(v) for v in r] for r in table])
    checks.append(("S&F ICC(1,1) = 0.17", round(one, 2) == 0.17))
    checks.append(("S&F ICC(1,4) = 0.44", round(many, 2) == 0.44))
    # The count-based binary ICC must equal the textbook form on the same ratings.
    binary = [[1, 1, 1], [0, 0, 0], [1, 1, 0], [0, 0, 1], [1, 1, 1], [0, 0, 0], [1, 0, 0]]
    groups = [(sum(r), len(r)) for r in binary]
    ref1, refk = icc1_values([[float(v) for v in r] for r in binary])
    checks.append(("binary ICC(1,1) matches the raw form", abs(icc1(groups) - ref1) < 1e-12))
    checks.append(("binary ICC(1,k) matches the raw form", abs(icc_k(groups) - refk) < 1e-12))
    perfect = [(3, 3)] * 5 + [(0, 3)] * 5
    checks.append(
        ("perfect agreement: ICC 1 and kappa 1", icc1(perfect) == 1 == fleiss_kappa(perfect))
    )
    anti = [(1, 3)] * 8  # every issue split the same way: less agreement than chance
    checks.append(
        (
            "all-1-of-3 issues: ICC -1/2 and kappa -1/2",
            abs(icc1(anti) + 0.5) < 1e-12 and abs(fleiss_kappa(anti) + 0.5) < 1e-12,
        )
    )
    checks.append(("no variance at all: kappa undefined", math.isnan(fleiss_kappa([(3, 3)] * 6))))
    checks.append(
        ("pair agreement of a 2-1 split is 1/3", abs(pair_agreement(2, 3) - 1 / 3) < 1e-12)
    )
    checks.append(("pair agreement of a unanimous issue is 1", pair_agreement(0, 3) == 1.0))
    checks.append(("average ranks for ties", _ranks([5.0, 1.0, 1.0, 9.0]) == [3.0, 1.5, 1.5, 4.0]))
    checks.append(
        ("Spearman of a monotone pair is 1", spearman([(1, 5), (2, 6), (3, 9), (4, 10)]) == 1.0)
    )
    checks.append(
        ("Spearman of a constant side is undefined", math.isnan(spearman([(1, 1), (2, 1), (3, 1)])))
    )
    checks.append(
        (
            "rating bins on six trials",
            [rating(k, 6) for k in range(7)]
            == ["unsolved", "hard", "hard", "moderate", "moderate", "easy", "easy"],
        )
    )
    est = bootstrap(perfect, icc1)
    checks.append(
        ("a perfect pool has a degenerate interval at 1", est.point == est.low == est.high == 1.0)
    )
    rng = random.Random(7)
    sim = [
        (sum(rng.random() < p for _ in range(3)), 3)
        for p in [rng.choice([0.05, 0.95]) for _ in range(60)]
    ]
    kappa, icc = fleiss_kappa(sim), icc1(sim)
    checks.append(("kappa and ICC(1,1) agree closely on bimodal data", abs(kappa - icc) < 0.05))
    bad = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(("PASS " if ok else "FAIL ") + name)
    print(f"{len(checks) - len(bad)}/{len(checks)} checks passed")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("scored", nargs="*", type=Path, help="one scored .jsonl per model")
    ap.add_argument("--md", type=Path, help="write or refresh the generated block in this file")
    ap.add_argument("--json", type=Path, help="write the per-issue records here")
    ap.add_argument("--self-test", action="store_true", help="run the built-in checks and exit")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if len(args.scored) < 2:
        ap.error("give at least two scored files (one per model)")
    models = [load_runs(p) for p in args.scored]
    block, payload = build(models)
    if args.md:
        write_block(args.md, block)
    else:
        print(block)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, indent=1, default=list) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
