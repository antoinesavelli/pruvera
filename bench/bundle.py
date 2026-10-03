"""A trial's artifact bundle: the files the scorer reads, hashed so a changed one is found.

A record stores the digest when the trial ends; scoring refuses a bundle that no longer matches
it. Records from before the digest existed carry none and are not checked.
Depends on: the standard library.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

FILES = ("changes.json", "diff.patch", "git_state.json", "status.txt", "transcript.jsonl")


def digest(art: Path) -> str:
    """sha256 over the names and bytes of the scorer's input files that exist."""
    h = hashlib.sha256()
    for name in FILES:
        path = art / name
        if path.is_file():
            data = path.read_bytes()
            h.update(f"{name}\0{len(data)}\0".encode() + data)
    return h.hexdigest()


def verify(art: Path, expected: str) -> str:
    """'' when the bundle matches (or the record predates digests), else why it does not."""
    if expected and digest(art) != expected:
        return f"{art.name}: the artifact bundle changed since the trial ended"
    return ""
