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
            for entry in json.load(resp).get("models", []):
                if entry.get("name") == model or entry.get("model") == model:
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


def gpu_residency() -> str:
    """The PROCESSOR column of `ollama ps` (for example '100% GPU'), or ''."""
    try:
        rows = subprocess.run(
            ["ollama", "ps"], capture_output=True, text=True, timeout=10, check=False
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return ""
    if len(rows) < 2:
        return ""
    start, end = rows[0].find("PROCESSOR"), rows[0].find("UNTIL")
    if start < 0:
        return ""
    return rows[1][start : end if end > start else None].strip()[:24]


@functools.cache
def opencode_version() -> str:
    if not OPENCODE.exists():
        return ""
    done = subprocess.run(
        [str(OPENCODE), "--version"], capture_output=True, text=True, timeout=30, check=False
    )
    return done.stdout.strip()
