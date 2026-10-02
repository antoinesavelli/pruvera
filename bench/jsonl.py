"""Read a JSON-lines results file: one record per non-blank line.

Depends on: the standard library.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_jsonl(path: Path, skip_bad: bool = False) -> list[dict[str, Any]]:
    """The records of a results file; `skip_bad` drops lines that are not JSON objects."""
    records: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            if skip_bad:
                continue
            raise
        if isinstance(record, dict):
            records.append(record)
        elif not skip_bad:
            raise ValueError(f"not a JSON object: {line[:60]}")
    return records
