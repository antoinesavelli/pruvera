"""What the local runtime says about itself: model digest and parameters, GPU use, Ollama's
version and start-up settings, opencode version.

Everything but `post_json` fails soft (an empty string or dict): a record notes what could be read,
never stops a trial. `post_json` raises, so its caller can tell a failure from an empty answer.
Depends on: the standard library; a local Ollama, `/proc` and the opencode binary (all optional).
"""

from __future__ import annotations

import functools
import json
import os
import socket
import subprocess
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

OPENCODE = Path.home() / ".opencode" / "bin" / "opencode"
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # no proxy variables
# Start-up settings that change how the server batches, caches or truncates, so whether a seed
# replays. Only these are read from its environment, never the rest (it can hold OLLAMA_API_KEY).
# Ollama 0.33.1 does not read OLLAMA_NUM_BATCH (the name is not in its binary); the service sets it.
SERVER_SETTINGS = (
    "OLLAMA_NUM_PARALLEL",
    "OLLAMA_KV_CACHE_TYPE",
    "OLLAMA_FLASH_ATTENTION",
    "OLLAMA_NUM_BATCH",
    "OLLAMA_CONTEXT_LENGTH",
)


def _open(request: str | urllib.request.Request, timeout: float) -> Any:
    """Open a request through the opener that ignores proxy variables."""
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


def post_json(
    path: str, body: Mapping[str, Any], timeout: float, port: int = 11434
) -> dict[str, Any]:
    """POST `body` as JSON to the local Ollama's `path` and return the reply object, or raise."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        json.dumps(dict(body)).encode(),
        {"Content-Type": "application/json"},
    )
    with _open(req, timeout) as resp:
        reply = json.load(resp)
    if not isinstance(reply, dict):
        raise ValueError(f"{path} replied with a {type(reply).__name__}, not a JSON object")
    return reply


def model_parameters(model: str, port: int = 11434) -> str:
    """Ollama's default sampling and context parameters for `model` (`/api/show`), or ''."""
    # Trials are unseeded: the model's own defaults (temperature, top_p, num_ctx) are the only
    # sampling settings there are, so they are part of what a record says about the run.
    try:
        reply = post_json("/api/show", {"model": model}, 5, port)
    except (OSError, ValueError):
        return ""
    return " ".join(str(reply.get("parameters", "")).split())


def ollama_version(port: int = 11434) -> str:
    """The version the Ollama on `port` reports (`/api/version`), or ''."""
    try:
        with _open(f"http://127.0.0.1:{port}/api/version", 3) as resp:
            return str(json.load(resp).get("version", ""))
    except (OSError, ValueError, AttributeError):
        return ""


def _listen_port(ollama_host: str) -> int | None:
    """The port a server started with this OLLAMA_HOST listens on, by Ollama's defaults, or None."""
    scheme, sep, rest = ollama_host.strip().partition("://")
    if not sep:
        scheme, rest = "", scheme
    default = {"http": 80, "https": 443}.get(scheme, 11434)
    hostport = rest.partition("/")[0]
    if hostport.startswith("["):  # [IPv6]:port
        hostport = hostport.partition("]")[2]
    elif hostport.count(":") > 1:  # a bare IPv6 address carries no port
        hostport = ""
    port = hostport.rpartition(":")[2] if ":" in hostport else ""
    if not port:
        return default
    return int(port) if port.isdigit() else None


def _serve_environments(proc: Path) -> list[dict[str, str]]:
    """OLLAMA_HOST and the SERVER_SETTINGS of each `ollama serve` whose environment is readable."""
    keep = {"OLLAMA_HOST", *SERVER_SETTINGS}
    found = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
            if argv[1:2] != [b"serve"] or Path(os.fsdecode(argv[0])).name != "ollama":
                continue
            raw = (entry / "environ").read_bytes()
        except OSError:  # it exited, or it runs as another user
            continue
        pairs = (item.partition("=") for item in os.fsdecode(raw).split("\0"))
        found.append({name: value for name, _, value in pairs if name in keep})
    return found


def ollama_server(port: int = 11434, proc: Path = Path("/proc")) -> dict[str, str]:
    """This machine's name and the SERVER_SETTINGS of the Ollama serving `port`, or {} if unknown.

    A setting the server's environment leaves unset is '' (that Ollama version's default applies).
    Unknown when no `ollama serve` here with a readable environment listens on `port` (it runs as
    another user, or the port is a tunnel to another machine), or when more than one claims it.
    """
    try:
        servers = [
            env
            for env in _serve_environments(proc)
            if _listen_port(env.get("OLLAMA_HOST", "")) == port
        ]
    except OSError:  # no /proc
        return {}
    if len(servers) != 1:
        return {}
    return {
        "host": socket.gethostname(),
        **{name: servers[0].get(name, "") for name in SERVER_SETTINGS},
    }


def _processor_cells(listing: str) -> dict[str, str]:
    """NAME -> PROCESSOR cell per row of `ollama ps` output; a repeated name keeps its first row."""
    rows = listing.splitlines()
    if not rows:
        return {}
    start = rows[0].find("PROCESSOR")
    if start < 0:
        return {}
    # the column ends where the next one (CONTEXT, else UNTIL) begins
    ends = [i for i in (rows[0].find("CONTEXT"), rows[0].find("UNTIL")) if i > start]
    end = min(ends) if ends else None
    cells: dict[str, str] = {}
    for row in rows[1:]:
        if row.strip():
            cells.setdefault(row.split()[0], row[start:end].strip()[:24])
    return cells


def loaded_models() -> dict[str, str]:
    """Each model `ollama ps` lists, by name, with its PROCESSOR cell (e.g. '100% GPU'), or {}."""
    try:
        listing = subprocess.run(
            ["ollama", "ps"], capture_output=True, text=True, timeout=10, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return _processor_cells(listing)


def gpu_residency(model: str) -> str:
    """The PROCESSOR column of `model`'s row in `ollama ps` (for example '100% GPU'), or ''."""
    # Only that model's own row counts: a trial also calls other models (opencode's `small_model`
    # titles a session), so the first row can belong to one of them. Not loaded -> '' (unknown).
    return loaded_models().get(model, "")


@functools.cache
def opencode_version() -> str:
    """The installed opencode's version, or an empty string if it is not installed."""
    if not OPENCODE.exists():
        return ""
    done = subprocess.run(
        [str(OPENCODE), "--version"], capture_output=True, text=True, timeout=30, check=False
    )
    return done.stdout.strip()
