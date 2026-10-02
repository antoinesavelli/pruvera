"""The gate's ledger: every candidate judged, so multiplicity is counted and the holdout used once.

A rule variant is tuned against known issues, and the more variants are tried, the likelier one
clears by chance. The ledger records each verdict (append-only JSONL). A candidate is its variant
name, whatever profile it ran on. Its family is every distinct variant judged so far, on any
split, and the family size widens the gate's intervals (Bonferroni). Any judgement whose
trials include a holdout issue uses up that variant's one look at the holdout, however the
profiles were named, and may not be a `--no-ledger` dry look. Rows carry a hash of the variant's
files, so a verdict can be tied to the text it judged. Depends on: bench.jsonl.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from bench.jsonl import read_jsonl

HOLDOUT = "holdout"
TUNE = "tune"
SETS = (HOLDOUT, TUNE, "holdout2", "tune2", "dev")


class LedgerError(RuntimeError):
    """The ledger forbids this judgement (the holdout was already used for this candidate)."""


def variant_of(profile: str) -> str:
    """The variant a profile name carries (`holdout+x@hist` gives `x`), or '' for a baseline."""
    return profile.partition("+")[2].split("@", 1)[0]


def base_of(profile: str) -> str:
    """The profile without its variant or history suffix (`tune+x@hist` gives `tune`)."""
    return profile.split("+", 1)[0].split("@", 1)[0]


def set_of(profile: str) -> str:
    """`holdout`, `tune` or `full`: the issue set a profile name is built from."""
    stem = base_of(profile)
    return stem if stem in SETS else "full"


def variant_hash(variants_dir: Path, variant: str) -> str:
    """Hash of a variant's files and `variant.toml` (empty when there is no such variant)."""
    root = variants_dir / variant
    if not variant or not root.is_dir():
        return ""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()[:16]


def read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return read_jsonl(path)


def family_size(entries: list[dict[str, Any]], baseline: str, candidate: str) -> int:
    """Distinct variants ever judged, this one included: every split tests the same claim."""
    names = {variant_of(e["candidate"]) for e in entries} - {""}
    return len(names | {variant_of(candidate)})


def used_gens(entry: dict[str, Any]) -> set[str]:
    """The holdout generations an entry used (old entries: `holdout_used` means generation 1)."""
    return set(entry.get("holdout_gens") or (["1"] if entry.get("holdout_used") else []))


def check_holdout(entries: list[dict[str, Any]], candidate: str, gens: set[str]) -> None:
    """A variant is judged on each holdout generation once; a changed variant needs a new name."""
    variant = variant_of(candidate)
    for e in entries:
        if variant_of(e["candidate"]) == variant and used_gens(e) & gens:
            raise LedgerError(
                f"{variant} already used its holdout look ({e['verdict']}, {e['date']}); "
                "a changed variant needs a new name"
            )


def record(path: Path, entry: dict[str, Any]) -> None:
    entry = {**entry, "date": time.strftime("%Y-%m-%d")}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")


def cleared(entries: list[dict[str, Any]], variant: str, current_hash: str) -> str:
    """'' when the variant has a CLEAR holdout verdict for its current files, else why not."""
    mine = [e for e in entries if variant_of(e["candidate"]) == variant]
    holdout = [e for e in mine if e.get("holdout_used")]
    verdicts = [e for e in holdout if e["verdict"] == "CLEAR"]
    problems = [
        (not mine, f"{variant} was never judged"),
        (not holdout, f"{variant} has no holdout verdict"),
        (not verdicts, f"{variant}: the holdout verdicts are {[e['verdict'] for e in holdout]}"),
        (
            not any(current_hash and e.get("variant_hash") == current_hash for e in verdicts),
            f"{variant}: its files changed since the CLEAR verdict (or are unhashed)",
        ),
    ]
    return next((why for failed, why in problems if failed), "")
