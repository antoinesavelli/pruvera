"""Where a fixture version's pieces live, and the facts every tool must agree on.

One place for the default version, the data-slice version, the real repo's path and a version's
pinned source commit (read from its manifest, never retyped), so a new fixture version is one
edit here and nothing drifts between the runner, the miner, the studies and the index.
Depends on: the standard library.

Code and catalogue (`bench/`, `issues/`, `variants/`, `experiments/`) are read from `ROOT`.
Everything a run reads or writes that is local or large (fixtures, artifacts, overlays, results)
lives under `STATE`, which is `ROOT` unless `AGENT_TESTING_STATE` names another checkout: a verdict
run can then execute from a clean `git worktree` of one commit and share the main checkout's state.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = Path(os.environ.get("AGENT_TESTING_STATE") or ROOT).resolve()
FIXTURES = STATE / "fixtures" / "paramo"
ARTIFACTS = STATE / "artifacts"
OVERLAYS = STATE / "overlays"
RESULTS = STATE / "results"
VERSION = "v2"
DATA_VERSION = "v3"  # v2 data plus synthetic aggregates and the public Nasdaq directory
REAL_REPO = Path("/mnt/ParamoStorage/Paramo")


def artifact_dir(recorded: str | Path) -> Path:
    """A trial's artifact directory; a recorded path that is gone is re-rooted under this repo."""
    path = Path(recorded)
    if path.is_dir() or "artifacts" not in path.parts:
        return path
    return STATE.joinpath(*path.parts[len(path.parts) - 1 - path.parts[::-1].index("artifacts") :])


def version_dir(version: str = VERSION) -> Path:
    return FIXTURES / "versions" / version


def tree(version: str = VERSION) -> Path:
    return version_dir(version) / "tree"


def venv(version: str = VERSION) -> Path:
    return FIXTURES / "venv" / version


def data_root() -> Path:
    return FIXTURES / "data" / DATA_VERSION / "root"


def rag_dir(version: str = VERSION) -> Path:
    return FIXTURES / "rag" / version


def find_profile_dir(tree_hash: str, version: str = VERSION) -> Path | None:
    """The built profile (current or superseded `profiles.old-*`) whose tree has this hash."""
    for manifest in sorted(version_dir(version).glob("profiles*/*/MANIFEST.json")):
        if json.loads(manifest.read_text()).get("tree_hash") == tree_hash:
            return manifest.parent
    return None


def source_commit(version: str = VERSION) -> str:
    """The real-repo commit this fixture version was built from."""
    return str(json.loads((version_dir(version) / "MANIFEST.json").read_text())["source_commit"])
