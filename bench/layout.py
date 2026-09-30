"""Where a fixture version's pieces live, and the facts every tool must agree on.

One place for the default version, the data-slice version, the real repo's path and a version's
pinned source commit (read from its manifest, never retyped), so a new fixture version is one
edit here and nothing drifts between the runner, the miner, the studies and the index.
Depends on: the standard library.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures" / "paramo"
VERSION = "v2"
DATA_VERSION = "v3"  # v2 data plus synthetic aggregates and the public Nasdaq directory
REAL_REPO = Path("/mnt/ParamoStorage/Paramo")


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


def source_commit(version: str = VERSION) -> str:
    """The real-repo commit this fixture version was built from."""
    return str(json.loads((version_dir(version) / "MANIFEST.json").read_text())["source_commit"])
