"""Decision tiers: which change to Paramo needs which evidence, and whether a verdict still holds.

`policy/tiers.toml` maps changed files to a tier by path. Tier 1 needs an `Evidence:` trailer
naming a registered experiment; tier 2 needs a `Gate-Verdict:` trailer naming a confirmatory CLEAR
whose scope (model digests, opencode version, fixture source commit) still matches today's.
`check` reports; it blocks only when asked to (`--mode block`), so it can run warn-first.
Depends on: bench.{gitutil,layout,modelinfo,registry}.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

from bench import gitutil, layout, modelinfo, registry

TIERS_FILE = layout.ROOT / "policy" / "tiers.toml"
TRAILER = re.compile(r"^(Evidence|Gate-Verdict):\s*(\S+)\s*$", re.M)


def load_tiers(path: Path = TIERS_FILE) -> dict[int, list[str]]:
    doc = tomllib.loads(path.read_text())
    return {1: list(doc["tier1"]["patterns"]), 2: list(doc["tier2"]["patterns"])}


def classify(files: list[str], tiers: dict[int, list[str]] | None = None) -> int:
    """The highest tier any changed file falls in (0 when none does)."""
    tiers = tiers or load_tiers()
    return max(
        (t for f in files for t, pats in tiers.items() if any(fnmatch.fnmatch(f, p) for p in pats)),
        default=0,
    )


def trailers(message: str) -> dict[str, str]:
    return {name: value for name, value in TRAILER.findall(message)}


def scope_problems(scope: dict[str, Any]) -> list[str]:
    """Why a verdict's scope no longer holds (an unreadable digest or version fails closed)."""
    problems = []
    for model, digests in scope.get("model_digests", {}).items():
        now = modelinfo.model_digest(model)
        if not now or now not in digests:
            problems.append(
                f"model {model}: digest is {now or 'unreadable'}, the verdict held for {digests}"
            )
    version = modelinfo.opencode_version()
    if version not in scope.get("opencode_version", []):
        problems.append(
            f"opencode is {version or 'unreadable'}, "
            f"the verdict held for {scope.get('opencode_version')}"
        )
    if layout.source_commit() not in scope.get("fixture_source_commit", []):
        problems.append("the fixture was rebuilt from another source commit since the verdict")
    return problems


def _events(experiment: str, root: Path) -> dict[str, dict[str, Any]]:
    """The first registered and judged rows of an experiment, by event name."""
    found: dict[str, dict[str, Any]] = {}
    for row in registry.read(root):
        if row.get("id") == experiment:
            found.setdefault(row["event"], row)
    return found


def clearance(experiment: str, root: Path = layout.ROOT) -> list[str]:
    """Why `experiment` does not clear a tier-2 change (empty when it does)."""
    rows = _events(experiment, root)
    if "registered" not in rows or "judged" not in rows:
        return [f"{experiment}: no judged experiment of that id"]
    kind, verdict = rows["registered"]["kind"], rows["judged"]["verdict"]
    if kind != "confirmatory" or verdict != "CLEAR":
        return [f"{experiment}: needs a confirmatory CLEAR, is {kind} {verdict}"]
    return scope_problems(rows["judged"]["scope"])


def evidence_problems(experiment: str, root: Path = layout.ROOT) -> list[str]:
    """Why `experiment` does not count as evidence for a tier-1 change."""
    mine = [r["event"] for r in registry.read(root) if r.get("id") == experiment]
    if "registered" not in mine:
        return [f"{experiment}: not a registered experiment"]
    return [f"{experiment}: abandoned"] if "abandoned" in mine else []


def check(files: list[str], message: str, root: Path = layout.ROOT) -> tuple[int, list[str]]:
    """(tier, problems) for one change: the evidence its tier asks for, named in its trailers."""
    tier = classify(files)
    found = trailers(message)
    if tier == 1 and "Evidence" not in found and "Gate-Verdict" not in found:
        return tier, ["tier 1 change: add an `Evidence: <experiment id>` trailer"]
    if tier == 1:
        return tier, evidence_problems(found.get("Evidence") or found["Gate-Verdict"], root)
    if tier == 2 and "Gate-Verdict" not in found:
        return tier, [
            "tier 2 change: add a `Gate-Verdict: <experiment id>` trailer (confirmatory CLEAR)"
        ]
    return tier, clearance(found["Gate-Verdict"], root) if tier == 2 else []


def check_range(repo: Path, rng: str, root: Path = layout.ROOT) -> list[str]:
    """Problems for every commit in `rng` of the repo whose rules are being changed."""
    problems: list[str] = []
    for sha in gitutil.text(repo, "rev-list", "--reverse", rng).split():
        files = gitutil.text(repo, "show", "--name-only", "--format=", sha).split("\n")
        tier, found = check(
            [f for f in files if f], gitutil.text(repo, "show", "-s", "--format=%B", sha), root
        )
        problems += [f"{sha[:10]} (tier {tier}): {p}" for p in found]
    return problems


def decision_row(experiment: str, root: Path = layout.ROOT) -> str:
    """The `docs/DECISIONS.md` row for a judged experiment, to paste (never written for you)."""
    judged = next(
        r for r in registry.read(root) if r.get("id") == experiment and r["event"] == "judged"
    )
    return (
        f"| D-NNN | {judged['date']} | Delegation rule change judged by experiment `{experiment}`: "
        f"{judged['verdict']} (success difference {judged['diff']:+.3f}, "
        f"alpha {judged['alpha_used']:.4f}) "
        f"| accepted | `pruvera/results/{judged['bundle']}/REPORT.md` |"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ck = sub.add_parser("check", help="evidence for the commits in a range of the Paramo repo")
    ck.add_argument("--repo", type=Path, required=True)
    ck.add_argument("--range", required=True, dest="rng")
    ck.add_argument("--mode", choices=("warn", "block"), default="warn")
    sub.add_parser("decision-row").add_argument("id")
    cl = sub.add_parser("clear", help="exit 0 only if the experiment still clears a tier-2 change")
    cl.add_argument("id")
    args = ap.parse_args(argv)
    if args.cmd == "decision-row":
        print(decision_row(args.id))
        return 0
    problems = clearance(args.id) if args.cmd == "clear" else check_range(args.repo, args.rng)
    print("\n".join(problems) or "ok")
    return 1 if problems and (args.cmd == "clear" or args.mode == "block") else 0


if __name__ == "__main__":
    sys.exit(main())
