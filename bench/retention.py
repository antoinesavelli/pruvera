"""What can be cleaned up, listed and never deleted: stale overlays and artifacts no result cites.

Read-only. A trial overlay older than `STALE_DAYS` (its trial finished long ago; kept overlays of
unusual trials are the ones worth a look) and an artifact directory whose trial id appears in no
results file (active or archived) are listed with their sizes; the owner deletes them.
Depends on: bench.{jsonl,layout}.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from bench import layout
from bench.jsonl import read_jsonl

STALE_DAYS = 2


def _size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())


def cited_trials(results: Path) -> set[str]:
    """Every trial id in any results file under `results`, archive included."""
    ids: set[str] = set()
    for path in results.rglob("*.jsonl"):
        if path.name.endswith(".scored.jsonl"):
            continue
        ids |= {str(r["trial_id"]) for r in read_jsonl(path, skip_bad=True) if "trial_id" in r}
    return ids


def stale_overlays(overlays: Path, now: float | None = None) -> list[dict[str, Any]]:
    """Overlay directories untouched for `STALE_DAYS`, oldest first (hidden names are skipped)."""
    cutoff = (now or time.time()) - STALE_DAYS * 86400
    found = [
        {"path": p.name, "days": round(((now or time.time()) - p.stat().st_mtime) / 86400, 1)}
        for p in sorted(overlays.iterdir())
        if p.is_dir() and not p.name.startswith(".") and p.stat().st_mtime < cutoff
    ]
    return sorted(found, key=lambda f: -float(f["days"]))


def orphan_artifacts(artifacts: Path, results: Path) -> list[str]:
    """Artifact directories that no results file cites."""
    cited = cited_trials(results)
    return sorted(p.name for p in artifacts.iterdir() if p.is_dir() and p.name not in cited)


def report(state: Path = layout.STATE) -> dict[str, Any]:
    """The cleanup candidates, with sizes for the overlays (artifact sizes are by count only)."""
    overlays = state / "overlays"
    stale = stale_overlays(overlays) if overlays.is_dir() else []
    for item in stale:
        item["bytes"] = _size(overlays / str(item["path"]))
    arts = state / "artifacts"
    orphans = orphan_artifacts(arts, state / "results") if arts.is_dir() else []
    return {"stale_overlays": stale, "orphan_artifacts": orphans}


def main() -> int:
    print(json.dumps(report(), indent=1))
    return 0
