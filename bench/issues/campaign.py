"""Mutation campaign: sample mutants of chosen modules and run each module's tests to classify them.

`killed` mutants fail at least one test (planted logic bugs a test catches); `survived` mutants pass
every test (candidate logic bugs no test catches, or equivalent mutants, so they need review).
Depends on: bench.layout, bench.issues.{check,mutate}; the fixture tree, venv and data slice.
"""

from __future__ import annotations

import json
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any

from bench import layout
from bench.issues import check, mutate

MAX_WORKERS = 4  # repo rule: never more than 5 concurrent jobs
PER_KIND = 3


def sample(source: str, seed: int, per_kind: int = PER_KIND) -> list[mutate.Mutant]:
    """A deterministic, kind-balanced sample of a file's mutants."""
    rng = random.Random(seed)
    by_kind: dict[str, list[mutate.Mutant]] = {}
    for m in mutate.candidates(source):
        by_kind.setdefault(m.operator, []).append(m)
    picked: list[mutate.Mutant] = []
    for kind in sorted(by_kind):
        pool = by_kind[kind]
        picked += rng.sample(pool, min(per_kind, len(pool)))
    return sorted(picked, key=lambda m: (m.line, m.col))


def evaluate(env: check.Env, module: str, test_file: str, seed: int = 1) -> dict[str, Any]:
    """Baseline the test file, then run it against each sampled mutant of `module`."""
    source = (env.tree / module).read_text()
    base = check.run_pytest(env, [test_file])
    out: dict[str, Any] = {"module": module, "test_file": test_file, "baseline_passed": base.passed}
    if not base.passed:
        out["mutants"] = []
        return out
    mutants = sample(source, seed)

    def one(m: mutate.Mutant) -> dict[str, Any]:
        res = check.run_pytest(env, [test_file], {module: mutate.apply(source, m)})
        return {**asdict(m), "killed": not res.passed, "failed": list(res.failed[:3])}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        out["mutants"] = list(pool.map(one, mutants))
    return out


def sibling_test(module: str) -> str:
    """The repo's convention: `a/b/c.py` is tested by `tests/a/b/test_c.py`."""
    path = Path(module)
    return f"tests/{path.parent}/test_{path.stem}.py"


def _options(argv: list[str]) -> tuple[str, list[str], bool]:
    """(output file name, the module specs, --force)."""
    force = "--force" in argv
    argv = [a for a in argv if a != "--force"]
    if argv[:1] == ["--out"]:
        return argv[1], argv[2:], force
    return "_campaign.json", argv, force


def main(argv: list[str]) -> int:
    """`campaign.py [--out FILE] module.py[:tests.py] ...`: sample mutants, run their tests."""
    if argv[:1] in (["-h"], ["--help"]):
        print(main.__doc__)
        return 0
    env = check.Env(layout.tree(), layout.venv(), layout.data_root())
    name, argv, force = _options(argv)
    if (layout.ROOT / "issues" / name).exists() and not force:
        print(f"issues/{name} exists (the seed reads it): pass --force to overwrite")
        return 1
    modules = argv or ["utils/price_ticks.py"]
    results = []
    for spec in modules:
        module, _, test_file = spec.partition(":")  # `module.py:tests/path.py` names the tests
        test_file = test_file or sibling_test(module)
        results.append(evaluate(env, module, test_file))
        killed = sum(m["killed"] for m in results[-1]["mutants"])
        print(f"{module}: {killed}/{len(results[-1]['mutants'])} killed", flush=True)
    out = layout.ROOT / "issues" / name
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(results, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
