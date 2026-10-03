"""The front door for studies: write a spec, register it, run it, abandon it, audit the registry.

`new` writes a spec template under `experiments/`; once it is committed, `register` makes it the
preregistration; `run` runs exactly what was registered (the only way to run a candidate) and
writes `results/experiments/<id>.jsonl`.
Depends on: bench.{layout,registry,runner}, bench.issues.trials.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from bench import layout, registry, runner
from bench.issues import trials

TEMPLATE = """\
id = "{id}"
question = ""  # one sentence: what would change if the answer is yes
kind = "exploratory"  # exploratory (no holdout issues) | confirmatory (holdout) | control
baseline = "dev"
candidate = ""  # "dev+<variant>"; leave empty for a baseline-only study
repeats = 6
decision_rule = ""  # the verdict rule and thresholds, fixed before the first trial
# [models]  # per-role model overrides for the candidate arm, e.g. git = "granite4.1:8b"
"""


def new(experiment_id: str, root: Path = layout.ROOT) -> Path:
    """Write a spec template; refuses to overwrite one."""
    path = root / "experiments" / f"{experiment_id}.toml"
    if path.exists():
        raise registry.RegistryError(f"{path.name} exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE.format(id=experiment_id))
    return path


def run(
    experiment_id: str,
    out: Path | None = None,
    *,
    n: int | None = None,
    only: list[str] | None = None,
    force: bool = False,
    root: Path = layout.ROOT,
) -> Path:
    """Run the registered experiment: both arms, the registered repeats, the registered models."""
    row = registry.registered(experiment_id, root)
    arms = {"baseline": runner.load_profile(layout.VERSION, row["baseline"])}
    if row["candidate"]:
        arms["candidate"] = runner.load_profile(layout.VERSION, row["candidate"])
        if arms["baseline"].issue_ids != arms["candidate"].issue_ids:
            raise ValueError("the two profiles plant different issues: the arms are not comparable")
    target = out or root / "results" / "experiments" / f"{experiment_id}.jsonl"
    return trials.run_arms(
        arms, n or row["repeats"], target, only=only, models=row["models"] or None,
        force=force, experiment=experiment_id,
    )  # fmt: skip


def _state(rows: list[dict[str, Any]], experiment: str) -> str:
    events = {r["event"] for r in rows if r.get("id") == experiment}
    return (
        "abandoned" if "abandoned" in events else "started" if "started" in events else "registered"
    )


def status(root: Path = layout.ROOT) -> list[dict[str, Any]]:
    """One line per experiment: its kind, the holdout looks it spent and where it stands."""
    rows = registry.read(root)
    return [
        {
            "id": r["id"],
            "kind": r["kind"],
            "holdout_gens": r["holdout_gens"],
            "state": _state(rows, r["id"]),
        }
        for r in rows
        if r["event"] == "registered"
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("new").add_argument("id")
    sub.add_parser("register").add_argument("spec", type=Path)
    go = sub.add_parser("run")
    go.add_argument("id")
    go.add_argument("--out", type=Path)
    go.add_argument("--n", type=int)
    go.add_argument("--only", nargs="*")
    ab = sub.add_parser("abandon")
    ab.add_argument("id")
    ab.add_argument("--reason", required=True)
    for name in ("status", "retro", "verify"):
        sub.add_parser(name)
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except registry.RegistryError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1


def _dispatch(args: argparse.Namespace) -> int:
    if args.cmd == "new":
        print(new(args.id))
    elif args.cmd == "register":
        print(json.dumps(registry.register(args.spec.resolve()), indent=1, sort_keys=True))
    elif args.cmd == "run":
        run(args.id, args.out, n=args.n, only=args.only)
    elif args.cmd == "abandon":
        registry.abandon(args.id, args.reason)
    elif args.cmd == "retro":
        print(f"{len(registry.retro())} studies recorded as registered after the data")
    elif args.cmd == "status":
        for line in status():
            print(json.dumps(line, sort_keys=True))
    else:
        problems = registry.audit()
        print("\n".join(problems) or "registry: chain intact, every candidate study covered")
        return 1 if problems else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
