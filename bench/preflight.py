"""Refuse to start a trial while the one GPU or the local agent stack is busy with something else.

Depends on: nvidia-smi and a local Ollama (both injectable); reads /proc, never pgrep -f.
"""

from __future__ import annotations

import os
import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

GPU_BUSY_PERCENT = (
    30  # a running routine or trial shows well above this; an idle card sits under 10
)
# argv[0] basenames and script names that mean "another agent run is live". Matched on argv,
# never on a substring of the command line, so a shell that merely mentions them does not count.
AGENT_BINARIES = frozenset({"opencode", "aider"})
BENCH_SCRIPTS = frozenset({"bench.py", "nav_bench.py", "nav_claude.py", "nested_canary.sh"})
INTERPRETERS = frozenset({"python", "python3", "bash", "sh"})


class PreflightError(RuntimeError):
    """One or more preconditions failed and force was not given."""


@dataclass(frozen=True)
class Problem:
    code: str  # stable identifier: agent_running, bench_running, gpu_busy, ollama_down
    message: str


def _cmdline(pid_dir: Path) -> list[str]:
    try:
        raw = (pid_dir / "cmdline").read_bytes()
    except OSError:
        return []
    return [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]


def _ancestors(proc_root: Path, pid: int) -> set[int]:
    """Our own pid and every parent, so a wrapper that launched us is never mistaken for a rival."""
    chain = {pid}
    while pid > 1:
        try:
            fields = (proc_root / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
            pid = int(fields[1])
        except (OSError, IndexError, ValueError):
            break
        chain.add(pid)
    return chain


def running_agents(proc_root: Path = Path("/proc"), me: int | None = None) -> list[Problem]:
    """Other agent or bench processes on this host, found by argv, not counting ourselves."""
    skip = _ancestors(proc_root, me if me is not None else os.getpid())
    found: list[Problem] = []
    for pid_dir in sorted(proc_root.iterdir()):
        if not pid_dir.name.isdigit() or int(pid_dir.name) in skip:
            continue
        argv = _cmdline(pid_dir)
        if not argv:
            continue
        exe = Path(argv[0]).name
        script = Path(argv[1]).name if len(argv) > 1 else ""
        if exe in AGENT_BINARIES:
            found.append(Problem("agent_running", f"pid {pid_dir.name}: {exe} is running"))
        elif exe in INTERPRETERS and script in BENCH_SCRIPTS:
            found.append(Problem("bench_running", f"pid {pid_dir.name}: {script} is running"))
    return found


def gpu_utilization() -> int | None:
    """GPU utilisation percent from nvidia-smi, or None if it cannot be read."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout.split()
        return int(out[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def ollama_reachable(port: int = 11434) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def problems(
    proc_root: Path = Path("/proc"),
    gpu: Callable[[], int | None] = gpu_utilization,
    ollama: Callable[[], bool] = ollama_reachable,
    me: int | None = None,
) -> list[Problem]:
    """Every reason not to start a trial right now; empty means clear."""
    found = running_agents(proc_root, me)
    util = gpu()
    if util is not None and util >= GPU_BUSY_PERCENT:
        found.append(Problem("gpu_busy", f"GPU utilisation is {util}% (limit {GPU_BUSY_PERCENT}%)"))
    if not ollama():
        found.append(Problem("ollama_down", "Ollama is not reachable on 127.0.0.1:11434"))
    return found


def check(
    force: bool = False,
    proc_root: Path = Path("/proc"),
    gpu: Callable[[], int | None] = gpu_utilization,
    ollama: Callable[[], bool] = ollama_reachable,
    me: int | None = None,
) -> list[Problem]:
    """Raise PreflightError listing every problem unless forced; a forced run still returns them."""
    found = problems(proc_root, gpu, ollama, me)
    if found and not force:
        raise PreflightError("; ".join(f"{p.code}: {p.message}" for p in found))
    return found
