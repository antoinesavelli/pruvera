"""Move results out of the active set without deleting anything, one reason per file.

`python3 -m bench.archive --reason "<why>" results/realism/study-3.jsonl ...` moves each file
(and the `.scored.jsonl` or `.rederived-*.json` files beside it) to `results/archive/<date>/`,
keeping its path under `results/`, and appends a line per file to `results/archive/INDEX.jsonl`.
It refuses to overwrite and never touches a ledger row, so a result that a verdict cites is
moved only by an explicit path. `bench.doctor` audits the active set strictly and lists the
archive on its own.
Depends on: bench.{layout,ledger}.
"""

from __future__ import annotations

import argparse
import datetime
import json
import shutil
import sys
from pathlib import Path

from bench import layout, ledger

ARCHIVE = "archive"


def _companions(path: Path) -> list[Path]:
    """The file and the derived files beside it (scored rows, dated re-derivations)."""
    siblings = [
        p
        for pattern in (f"{path.stem}.scored.jsonl", f"{path.stem}.rederived-*.json")
        for p in path.parent.glob(pattern)
    ]
    return [path, *sorted(siblings)]


def _ledger_files(results: Path) -> set[str]:
    """File names the gate ledger's rows were judged on."""
    return {Path(e["results"]).name for e in ledger.read(results / "gate" / "ledger.jsonl")}


def _plan_moves(files: list[Path], results: Path, day: str) -> list[tuple[Path, Path]]:
    """(source, destination) per file and companion; refuses anything that must stay or clash."""
    named = dict.fromkeys(p for f in files for p in _companions(f))  # once each, in order
    moves = [(p, results / ARCHIVE / day / p.relative_to(results)) for p in named]
    cited = _ledger_files(results)
    for src, dst in moves:
        if ARCHIVE in src.relative_to(results).parts:
            raise ValueError(f"{src} is already archived")
        if src.name in cited:
            raise ValueError(f"{src.name} is cited by a ledger row: a verdict's inputs stay active")
        if dst.exists():
            raise FileExistsError(f"{dst} exists: nothing is overwritten")
    return moves


def archive(
    files: list[Path], reason: str, results: Path = layout.ROOT / "results", day: str = ""
) -> list[Path]:
    """Move `files` and their companions under `<results>/archive/<day>/`; returns the new paths."""
    day = day or datetime.date.today().isoformat()
    moves = _plan_moves(files, results, day)
    index = results / ARCHIVE / "INDEX.jsonl"
    index.parent.mkdir(parents=True, exist_ok=True)
    with index.open("a") as out:
        for src, dst in moves:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(src, dst)
            row = {"file": str(src.relative_to(results)), "date": day, "reason": reason}
            out.write(json.dumps(row, sort_keys=True) + "\n")
    return [dst for _, dst in moves]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--results", type=Path, default=layout.ROOT / "results")
    ap.add_argument("--reason", required=True, help="one line: why this leaves the active set")
    args = ap.parse_args(argv)
    for dst in archive([f.resolve() for f in args.files], args.reason, args.results.resolve()):
        print(dst)
    return 0


if __name__ == "__main__":
    sys.exit(main())
