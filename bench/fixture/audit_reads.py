"""Log data-root paths a test run tries to open or list that are missing (synthetic-data step D1).

A pytest plugin (`-p audit_reads`, bound into the sandbox) that installs an audit hook: one JSON
line per missing path, tagged with the running test. For an inventory run only, never a trial.
Depends on: stdlib only.
"""

from __future__ import annotations

import json
import os
import sys

DATA_ROOTS = ("/mnt/ParamoStorage/trading", "/mnt/ParamoStorage/archive")
EVENTS = {"open", "os.listdir", "os.scandir"}
LOG = os.environ.get("AUDIT_READS_LOG", "/tmp/audit_reads.jsonl")
_current = {"test": ""}
_seen: set[tuple[str, str]] = set()


def _hook(event: str, args: tuple[object, ...]) -> None:
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


def pytest_runtest_setup(item: object) -> None:
    """Pytest hook: note the test about to run, so each missing path is tagged with it."""
    _current["test"] = getattr(item, "nodeid", "")


sys.addaudithook(_hook)
