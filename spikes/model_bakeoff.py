"""One-off: run the shared-tree commit issues on each model under two rule arms
(plans/MODEL_BAKEOFF_COMMIT.md).

Depends on: bench.issues.trials, bench.runner, bench.layout, bench.gate. Run from the repo root:
    python3 spikes/model_bakeoff.py <model> [<model> ...] [--n 4]
Writes results/bakeoff/commit-<model>.jsonl (both arms interleaved) and prints outcome counts.
"""

from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench import gate, layout, runner  # noqa: E402
from bench.issues import trials  # noqa: E402

PLAIN, TOOL = "tune+rules-after", "tune+commit-tool-2"
ISSUES = [
    "hand-scope-peer-staged",
    "hand-scope-peer-untracked",
    "hand-scope-quiet-peer-staged",
    "hand-scope-quiet-peer-untracked",
]


def bake(model: str, n: int) -> Path:
    out = ROOT / "results" / "bakeoff" / f"commit-{model.replace('/', '_').replace(':', '_')}.jsonl"
    arms = {
        "baseline": runner.load_profile(layout.VERSION, PLAIN),
        "candidate": runner.load_profile(layout.VERSION, TOOL),
    }
    trials.run_arms(arms, n, out, only=ISSUES, models={"git": model})
    return out


def summarize(model: str, out: Path) -> None:
    rows = gate.score_arms(out, PLAIN, TOOL)
    counts: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    for r in rows:
        counts[r["arm"]][r.get("outcome") or "?"] += 1
    for arm, label in (("baseline", "plain"), ("candidate", "tool ")):
        print(f"{model:<42} {label} n={sum(counts[arm].values()):<3} {dict(counts[arm])}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("models", nargs="+")
    ap.add_argument("--n", type=int, default=4)
    args = ap.parse_args()
    for model in args.models:
        summarize(model, bake(model, args.n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
