"""Sandbox for one agent trial: allowlisted bwrap root, read-only base under an overlay, no egress.

Depends on: bwrap and socat on the host; bench.ollama_filter; a trial directory the caller owns.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from bench import ollama_filter

HOME = "/home/trial"
# The fixture is mounted at the real repo path, so absolute paths in docs, config and agent habits
# hold, and models see the same 26-character prefix as in a real session.
WORKDIR = "/mnt/ParamoStorage/Paramo"
VENV_DIR = f"{WORKDIR}/.venv"  # where the project keeps its venv, so `.venv/bin/python` works
OLLAMA_PORT = 11434
# Host files a trial legitimately needs (name resolution, TLS roots, uid lookup, dynamic linker).
# Everything else under /etc is absent: an allowlist, not a mask, so nothing leaks by default.
ETC_ALLOW = (
    "resolv.conf",
    "hosts",
    "passwd",
    "group",
    "nsswitch.conf",
    "ld.so.cache",
    "localtime",
    "ssl",
    "ca-certificates",
    "alternatives",
)
Net = Literal["none", "ollama", "host"]


class SandboxError(RuntimeError):
    """The sandbox cannot be built or a precondition failed."""


@dataclass(frozen=True)
class Spec:
    """Everything one trial needs; the caller creates upper/work/xdg empty on the trial's disk."""

    base: Path  # read-only fixture tree, including its .git
    upper: Path  # overlay upper dir: every write a trial makes lands here
    work: Path  # overlay workdir: empty, same filesystem as upper
    xdg: Path  # trial-private config/data/state root (writable)
    ro_binds: tuple[tuple[Path, str], ...] = ()  # (host path, path inside): venv, opencode, data
    rw_binds: tuple[tuple[Path, str], ...] = ()  # writable binds, for builders only (never a trial)
    data_base: Path | None = (
        None  # read-only data slice, mounted at data_dest with throwaway writes
    )
    data_dest: str = "/mnt/ParamoStorage/trading"  # the real default data root, so no overrides
    env: Mapping[str, str] = field(default_factory=dict)
    net: Net = "ollama"
    ollama_port: int = OLLAMA_PORT
    ollama_models: frozenset[str] = frozenset()  # models a trial may run; empty allows any


def _usr_layout() -> list[str]:
    """Recreate the merged-/usr layout: symlink what the host symlinks, bind what it does not."""
    argv = ["--ro-bind", "/usr", "/usr"]
    for name in ("bin", "sbin", "lib", "lib64"):
        host = Path("/") / name
        if host.is_symlink():
            argv += ["--symlink", os.readlink(host), f"/{name}"]
        elif host.exists():
            argv += ["--ro-bind", str(host), f"/{name}"]
    return argv


def _etc_layout() -> list[str]:
    argv: list[str] = []
    for name in ETC_ALLOW:
        host = Path("/etc") / name
        if host.exists():
            argv += ["--ro-bind", str(host.resolve()), f"/etc/{name}"]
    return argv


def trial_env(spec: Spec) -> dict[str, str]:
    """The only environment a trial sees; the host's is never inherited."""
    env = {
        "PATH": "/opt/bin:/usr/local/bin:/usr/bin:/bin",  # /opt/bin: where ro_binds put tools
        "HOME": HOME,
        "XDG_CONFIG_HOME": f"{HOME}/.config",
        "XDG_DATA_HOME": f"{HOME}/.local/share",
        "XDG_STATE_HOME": f"{HOME}/.local/state",
        "XDG_CACHE_HOME": f"{HOME}/.cache",
        "TMPDIR": "/tmp",
        "LANG": "C.UTF-8",
        "TERM": "dumb",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPYCACHEPREFIX": "/tmp/pycache",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    env.update(spec.env)
    return env


def build_argv(spec: Spec, cmd: Sequence[str], proxy_sock: Path | None = None) -> list[str]:
    """Pure function: the full bwrap command line for one trial (unit-testable without bwrap)."""
    if spec.net == "ollama" and proxy_sock is None:
        raise SandboxError("net='ollama' needs the host-side proxy socket")
    argv = ["bwrap", "--die-with-parent", "--new-session", "--unshare-user", "--unshare-pid"]
    argv += ["--unshare-ipc", "--unshare-uts", "--unshare-cgroup-try"]
    if spec.net != "host":
        argv.append("--unshare-net")
    argv += _usr_layout() + _etc_layout()
    argv += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", HOME]
    argv += ["--overlay-src", str(spec.base), "--overlay", str(spec.upper), str(spec.work), WORKDIR]
    for sub, dest in (("config", ".config"), ("data", ".local/share"), ("state", ".local/state")):
        argv += ["--bind", str(spec.xdg / sub), f"{HOME}/{dest}"]
    for host, inside in spec.ro_binds:
        argv += ["--ro-bind", str(host), inside]
    for host, inside in spec.rw_binds:
        argv += ["--bind", str(host), inside]
    if spec.data_base is not None:
        argv += ["--overlay-src", str(spec.data_base), "--tmp-overlay", spec.data_dest]
        argv += ["--tmpfs", "/mnt/ParamoStorage/archive"]
    if proxy_sock is not None:
        argv += ["--ro-bind", str(proxy_sock), "/run/ollama.sock"]
    argv += ["--chdir", WORKDIR, "--clearenv"]
    for key, value in trial_env(spec).items():
        argv += ["--setenv", key, value]
    if spec.net == "ollama":
        # Loopback inside the new netns is empty: bridge the one allowed service in from the host.
        bridge = f"socat TCP-LISTEN:{spec.ollama_port},bind=127.0.0.1,fork,reuseaddr "
        bridge += 'UNIX-CONNECT:/run/ollama.sock & exec "$@"'
        return [*argv, "sh", "-c", bridge, "sandbox", *cmd]
    return [*argv, *cmd]


@contextlib.contextmanager
def ollama_proxy(port: int = OLLAMA_PORT, models: frozenset[str] = frozenset()) -> Iterator[Path]:
    """Host-side unix-socket bridge to Ollama, filtered to inference calls (`ollama_filter`)."""
    with tempfile.TemporaryDirectory(prefix="ollama-proxy-") as d:
        with ollama_filter.serve(Path(d) / "ollama.sock", port, models) as sock:
            yield sock


def run(
    spec: Spec, cmd: Sequence[str], timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one command inside the sandbox; the caller decides what a non-zero exit means."""
    if shutil.which("bwrap") is None:
        raise SandboxError("bwrap not found")
    check_layout(spec)
    with contextlib.ExitStack() as stack:
        sock = (
            stack.enter_context(ollama_proxy(spec.ollama_port, spec.ollama_models))
            if spec.net == "ollama"
            else None
        )
        return subprocess.run(
            build_argv(spec, cmd, sock),
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            check=False,
        )


@contextlib.contextmanager
def popen(spec: Spec, cmd: Sequence[str]) -> Iterator[subprocess.Popen[str]]:
    """Start one command inside the sandbox and stream it; the sandbox dies with the process."""
    if shutil.which("bwrap") is None:
        raise SandboxError("bwrap not found")
    check_layout(spec)
    with contextlib.ExitStack() as stack:
        sock = (
            stack.enter_context(ollama_proxy(spec.ollama_port, spec.ollama_models))
            if spec.net == "ollama"
            else None
        )
        proc = subprocess.Popen(
            build_argv(spec, cmd, sock),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            errors="replace",
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )
        try:
            yield proc
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)


def check_layout(spec: Spec) -> None:
    """Refuse to start on a layout that would let a write escape the overlay or share state."""
    dirs = [("base", spec.base), ("upper", spec.upper), ("work", spec.work)]
    if spec.data_base is not None:
        dirs.append(("data_base", spec.data_base))
    for label, path in dirs:
        if not path.is_dir():
            raise SandboxError(f"{label} is not a directory: {path}")
    if {p.name for p in spec.work.iterdir()} - {"work"}:
        # The kernel keeps its own `work` subdir between mounts; anything else is foreign.
        raise SandboxError("overlay workdir must start empty")
    if spec.upper.stat().st_dev != spec.work.stat().st_dev:
        raise SandboxError("overlay upper and work must be on the same filesystem")
    for sub in ("config", "data", "state"):
        if not (spec.xdg / sub).is_dir():
            raise SandboxError(f"xdg/{sub} missing under {spec.xdg}")
    trial_roots = {spec.upper.resolve(), spec.work.resolve(), spec.xdg.resolve()}
    if spec.base.resolve() in trial_roots or len(trial_roots) != 3:
        raise SandboxError("base, upper, work and xdg must be four distinct directories")


def tree_hash(root: Path, exclude_top: tuple[str, ...] = ()) -> str:
    """Content hash of a tree (paths, modes, bytes); a base that drifts changes it."""
    # `exclude_top` skips top-level entries such as `.git`, whose index holds per-machine stat data.
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.relative_to(root).parts[0] in exclude_top:
            continue
        rel = path.relative_to(root).as_posix().encode()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            digest.update(b"L" + rel + os.readlink(path).encode())
        elif stat.S_ISREG(info.st_mode):
            digest.update(b"F" + rel + f"{info.st_mode & 0o111}".encode() + path.read_bytes())
        elif stat.S_ISDIR(info.st_mode):
            digest.update(b"D" + rel)
    return digest.hexdigest()


def git_state_hash(tree: Path) -> str:
    """Hash of everything under `.git` but the index: config, hooks, refs and objects."""
    # The index holds per-machine stat data; the rest must match so a host-side change is caught.
    digest = hashlib.sha256()
    git = tree / ".git"
    for path in sorted(git.rglob("*")):
        rel = path.relative_to(git).as_posix()
        if rel == "index" or not path.is_file():
            continue
        digest.update(b"G" + rel.encode() + f"{path.lstat().st_mode & 0o111}".encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def fingerprint(root: Path) -> str:
    """Cheap identity of a big read-only directory: its paths, sizes and modes, not its bytes."""
    # Catches a swapped, added or truncated file in a venv or data slice without reading gigabytes.
    digest = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            info = path.lstat()
            rel = path.relative_to(root).as_posix()
            digest.update(f"{rel}\0{info.st_size}\0{info.st_mode & 0o7777}\n".encode())
    return digest.hexdigest()


def verify_base(base: Path, expected: str, exclude_top: tuple[str, ...] = ()) -> None:
    """Refuse to run on a base whose tree hash differs from its manifest."""
    actual = tree_hash(base, exclude_top)
    if actual != expected:
        raise SandboxError(f"base drifted: expected {expected[:12]}, got {actual[:12]}")


def overlay_changes(upper: Path) -> dict[str, list[str]]:
    """What a trial changed, read from the overlay upper dir: written vs deleted (whiteouts)."""
    changes: dict[str, list[str]] = {"written": [], "deleted": []}
    for path in sorted(upper.rglob("*")):
        info = path.lstat()
        rel = path.relative_to(upper).as_posix()
        if stat.S_ISCHR(info.st_mode) and info.st_rdev == 0:
            changes["deleted"].append(rel)
        elif stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            changes["written"].append(rel)
    return changes


def freeze(tree: Path) -> int:
    """Make a base tree read-only for everyone (files keep their execute bits); returns entries."""
    # A stray `ruff` or `pytest` run inside a base once wrote a cache into it; with no write bit
    # the tool fails instead of silently drifting the base. Symlinks are left alone.
    count = 0
    for dirpath, dirnames, filenames in os.walk(tree):
        for name in (*dirnames, *filenames):
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            mode = stat.S_IMODE(path.lstat().st_mode)
            path.chmod(mode & ~0o222)
            count += 1
    tree.chmod(stat.S_IMODE(tree.lstat().st_mode) & ~0o222)
    return count


def remove_trial_dirs(*dirs: Path) -> None:
    """Delete overlay dirs; the kernel leaves an unreadable workdir/work, so reopen it first."""
    for root in dirs:
        with contextlib.suppress(OSError):
            root.chmod(0o700)
        for dirpath, dirnames, _ in os.walk(root):
            for name in dirnames:
                target = Path(dirpath) / name
                if target.is_symlink():
                    continue  # chmod follows links: a trial-made link must not reach the host
                with contextlib.suppress(OSError):
                    target.chmod(0o700)
        shutil.rmtree(root, ignore_errors=True)
