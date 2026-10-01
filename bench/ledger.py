"""The gate's ledger: every candidate judged, so multiplicity is counted and the holdout used once.

A rule variant is tuned against known issues, and the more variants are tried, the likelier one
clears by chance. The ledger records each verdict (append-only JSONL) per baseline and issue set.
The size of that family widens the gate's intervals (Bonferroni), and a candidate may be judged
on the holdout set only once. Depends on: the standard library.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

HOLDOUT = "holdout"
TUNE = "tune"


class LedgerError(RuntimeError):
    """The ledger forbids this judgement (the holdout was already used for this candidate)."""


def set_of(profile: str) -> str:
    """`holdout`, `tune` or `full`: the issue set a profile name (`holdout+x`, `tune@hist`) is."""
    stem = profile.split("+", 1)[0].split("@", 1)[0]
    return stem if stem in (HOLDOUT, TUNE) else "full"


def read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def family_size(
    entries: list[dict[str, Any]], baseline: str, candidate: str, issue_set: str
) -> int:
    """Distinct candidates judged against `baseline` on `issue_set`, this one included."""
    names = {e["candidate"] for e in entries if e["baseline"] == baseline and e["set"] == issue_set}
    return len(names | {candidate})


def check_holdout(entries: list[dict[str, Any]], baseline: str, candidate: str) -> None:
    """A candidate is judged on the holdout once; a changed variant must be a new name."""
    for e in entries:
        if e["set"] == HOLDOUT and e["baseline"] == baseline and e["candidate"] == candidate:
            raise LedgerError(
                f"{candidate} was already judged on the holdout ({e['verdict']}, {e['date']}); "
                "a changed variant needs a new name"
            )


def record(path: Path, entry: dict[str, Any]) -> None:
    entry = {**entry, "date": time.strftime("%Y-%m-%d")}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
