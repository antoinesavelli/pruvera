"""What the local runtime says about itself: model digest and parameters, GPU use, opencode version.

Everything fails soft (an empty string): a record notes what could be read, never stops a trial.
Depends on: the standard library; a local Ollama and the opencode binary (both optional).
"""

from __future__ import annotations

import functools
import json
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

OPENCODE = Path.home() / ".opencode" / "bin" / "opencode"
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # no proxy variables


def _open(request: str | urllib.request.Request, timeout: float) -> Any:
    return _OPENER.open(request, timeout=timeout)


def model_digest(model: str, port: int = 11434) -> str:
    """Ollama's digest for `model`, or '' if it cannot be read."""
    try:
        with _open(f"http://127.0.0.1:{port}/api/tags", 3) as resp:
            names = (model, f"{model}:latest")  # Ollama tags a bare name as `:latest`
            for entry in json.load(resp).get("models", []):
                if entry.get("name") in names or entry.get("model") in names:
                    return str(entry.get("digest", ""))
    except (OSError, ValueError, AttributeError):
        pass
    return ""


def model_parameters(model: str, port: int = 11434) -> str:
    """Ollama's default sampling and context parameters for `model` (`/api/show`), or ''."""
    # Trials are unseeded: the model's own defaults (temperature, top_p, num_ctx) are the only
    # sampling settings there are, so they are part of what a record says about the run.
    body = json.dumps({"model": model}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/show", body, {"Content-Type": "application/json"}
    )
    try:
        with _open(req, 5) as resp:
            return " ".join(str(json.load(resp).get("parameters", "")).split())
    except (OSError, ValueError):
        return ""


def gpu_residency(model: str) -> str:
    """The PROCESSOR column of `model`'s row in `ollama ps` (for example '100% GPU'), or ''."""
    # Only that model's own row counts: a trial also calls other models (opencode's `small_model`
    # titles a session), so the first row can belong to one of them. Not loaded -> '' (unknown).
    try:
        rows = subprocess.run(
            ["ollama", "ps"], capture_output=True, text=True, timeout=10, check=False
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return ""
    if not rows:
        return ""
    start = rows[0].find("PROCESSOR")
    if start < 0:
        return ""
    # the column ends where the next one (CONTEXT, else UNTIL) begins
    ends = [i for i in (rows[0].find("CONTEXT"), rows[0].find("UNTIL")) if i > start]
    end = min(ends) if ends else None
    for row in rows[1:]:
        if row.split(maxsplit=1)[:1] == [model]:
            return row[start:end].strip()[:24]
    return ""


@functools.cache
def opencode_version() -> str:
    if not OPENCODE.exists():
        return ""
    done = subprocess.run(
        [str(OPENCODE), "--version"], capture_output=True, text=True, timeout=30, check=False
    )
    return done.stdout.strip()
