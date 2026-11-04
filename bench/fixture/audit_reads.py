"""Log data-root paths a test run tries to open or list that are missing (synthetic-data step D1).

A pytest plugin (`-p audit_reads`, bound into the sandbox) that installs an audit hook: one JSON
line per missing path, tagged with the running test. For an inventory run only, never a trial.
The data roots to watch come from `AUDIT_READS_ROOTS` (`os.pathsep`-separated), set by whoever
runs the inventory: the plugin runs inside the sandbox, where `bench` and the paths file do not
exist, and a generic default would watch the wrong root and read as "nothing is missing", so a
run without it fails.
Depends on: stdlib only.
"""

from __future__ import annotations

import json
import os
import sys

DATA_ROOTS = tuple(p for p in os.environ.get("AUDIT_READS_ROOTS", "").split(os.pathsep) if p)
EVENTS = {"open", "os.listdir", "os.scandir"}
LOG = os.environ.get("AUDIT_READS_LOG", "/tmp/audit_reads.jsonl")
_current = {"test": ""}
_seen: set[tuple[str, str]] = set()


def _hook(event: str, args: tuple[object, ...]) -> None:
    """Audit hook: log, once per test, a missing data-root path that was opened or listed."""
    if event not in EVENTS or not args:
        return
    path = args[0]
    if isinstance(path, bytes):
        path = path.decode("utf-8", "replace")
    if not isinstance(path, str) or not path.startswith(DATA_ROOTS) or os.path.exists(path):
        return
    key = (_current["test"], path)
    if key in _seen:
        return
    _seen.add(key)
    with open(LOG, "a") as fh:
        fh.write(json.dumps({"test": _current["test"], "path": path}) + "\n")


def pytest_configure(config: object) -> None:
    """Pytest hook: refuse to run with no roots to watch."""
    if not DATA_ROOTS:
        raise RuntimeError("audit_reads: set AUDIT_READS_ROOTS to the data roots to watch")


def pytest_runtest_setup(item: object) -> None:
    """Pytest hook: note the test about to run, so each missing path is tagged with it."""
    _current["test"] = getattr(item, "nodeid", "")


sys.addaudithook(_hook)
