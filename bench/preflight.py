"""Refuse to start a trial while the one GPU or the local agent stack is busy with something else.

Depends on: nvidia-smi and a local Ollama (both injectable); reads /proc, never pgrep -f.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import socket
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

GPU_BUSY_PERCENT = (
    30  # a running routine or trial shows well above this; an idle card sits under 10
)
# argv[0] basenames and script names that mean "another agent run is live". Matched on argv,
# never on a substring of the command line, so a shell that merely mentions them does not count.
AGENT_BINARIES = frozenset({"opencode", "aider"})
BENCH_SCRIPTS = frozenset({"bench.py", "nav_bench.py", "nav_claude.py", "nested_canary.sh"})
# `python -m <module>` entry points of this harness that start trials.
BENCH_MODULES = frozenset(
    {
        "bench.cli",
        "bench.realism",
        "bench.rag.experiment",
        "bench.issues.campaign",
        "bench.issues.trials",
        "bench.issues.mine",
        "bench.gate",
    }
)
INTERPRETER = re.compile(r"python[0-9.]*|bash|sh|env")
HARNESS_SCRIPT_SUFFIXES = ("bench/cli.py", "bench/realism.py", "bench/gate.py")


SESSION_LOCK = Path(__file__).resolve().parents[1] / "overlays" / ".session.lock"


@contextlib.contextmanager
def session_lock(path: Path | None = None) -> Iterator[None]:
    """Hold an exclusive lock for a whole run of trials; a second run fails fast, not silently."""
    # Two campaigns could otherwise both pass the process scan in the gap between trials.
    path = path or SESSION_LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise PreflightError(f"another run of trials holds {path}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class PreflightError(RuntimeError):
    """One or more preconditions failed and force was not given."""


@dataclass(frozen=True)
class Problem:
    code: str  # stable id: agent_running, bench_running, gpu_busy, gpu_unreadable, ollama_down
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


WRAPPERS = frozenset({"env", "timeout", "nice", "nohup", "stdbuf", "time", "ionice", "chrt"})
VALUE_FLAGS = frozenset(
    {"-X", "-W", "-c", "--check-hash-based-pycs"}
)  # options that take an argument


def _unwrap(argv: list[str]) -> list[str]:
    """argv with leading wrapper commands (`env A=1`, `timeout 5`, `nice -n 5`) removed."""
    i = 0
    while i < len(argv) and Path(argv[i]).name in WRAPPERS:
        i += 1
        while i < len(argv) and not INTERPRETER.fullmatch(Path(argv[i]).name):
            if Path(argv[i]).name in WRAPPERS:
                break
            i += 1
    return argv[i:] if i < len(argv) else argv


def _python_target(argv: list[str]) -> tuple[str, str]:
    """(module, script) an interpreter command line runs; either may be empty."""
    args = _unwrap(argv)[1:]
    skip = False
    for i, arg in enumerate(args):
        if skip:
            skip = False
        elif arg in VALUE_FLAGS:
            skip = True
        elif re.fullmatch(r"-[A-Za-z]*m", arg) and i + 1 < len(args):  # `-m`, `-um`, `-Im`
            return args[i + 1], ""
        elif arg.startswith("-m") and len(arg) > 2:
            return arg[2:], ""
        elif not arg.startswith("-"):
            return "", arg
    return "", ""


def _classify(pid: str, argv: list[str]) -> Problem | None:
    """The problem a process with this argv represents, if it is another agent or bench run."""
    argv = _unwrap(argv)
    exe = Path(argv[0]).name
    if exe in AGENT_BINARIES:
        return Problem("agent_running", f"pid {pid}: {exe} is running")
    if not (INTERPRETER.fullmatch(exe)):
        return None
    module, script = _python_target(argv)
    if module in BENCH_MODULES and "calibrate" not in argv:  # a pure simulation uses no GPU
        return Problem("bench_running", f"pid {pid}: {module} is running")
    name = Path(script).name
    harness_script = script.endswith(HARNESS_SCRIPT_SUFFIXES)
    if name in BENCH_SCRIPTS or harness_script:
        return Problem("bench_running", f"pid {pid}: {name} is running")
    return None


def running_agents(proc_root: Path = Path("/proc"), me: int | None = None) -> list[Problem]:
    """Other agent or bench processes on this host, found by argv, not counting ourselves."""
    skip = _ancestors(proc_root, me if me is not None else os.getpid())
    found: list[Problem] = []
    for pid_dir in sorted(proc_root.iterdir()):
        if not pid_dir.name.isdigit() or int(pid_dir.name) in skip:
            continue
        argv = _cmdline(pid_dir)
        problem = _classify(pid_dir.name, argv) if argv else None
        if problem is not None:
            found.append(problem)
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
    if util is None:
        found.append(Problem("gpu_unreadable", "nvidia-smi gave no reading: contention unknown"))
    elif util >= GPU_BUSY_PERCENT:
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


def wait_clear(
    timeout: float = 180.0,
    interval: float = 2.0,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    proc_root: Path = Path("/proc"),
    gpu: Callable[[], int | None] = gpu_utilization,
    ollama: Callable[[], bool] = ollama_reachable,
    me: int | None = None,
) -> list[Problem]:
    """Poll until nothing blocks a trial or `timeout` passes; return what still blocks."""
    # For back-to-back trials, where the previous trial's model is still finishing on the GPU.
    deadline = clock() + timeout
    while True:
        found = problems(proc_root, gpu, ollama, me)
        if not found or clock() >= deadline:
            return found
        sleep(interval)
