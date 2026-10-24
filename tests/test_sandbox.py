"""Tests for bench.sandbox: argv construction (pure) and a scripted escape test (real bwrap).

Every write attempt targets a temp path or a path the sandbox should not contain; nothing here can
touch real data even if the sandbox failed, because no real path is ever a write target.
"""

from __future__ import annotations

import dataclasses
import os
import re
import shutil
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from bench import runner, sandbox
from bench.sandbox import SandboxError, Spec
from tests.helpers import bwrap_works, needs_bwrap, sh

_bwrap_works = bwrap_works


def _ollama_up() -> bool:
    """Returns True when a local Ollama server accepts a connection on its port, else False."""
    try:
        with socket.create_connection(("127.0.0.1", sandbox.OLLAMA_PORT), timeout=1):
            return True
    except OSError:
        return False


@pytest.fixture
def spec(tmp_path: Path) -> Iterator[Spec]:
    """Builds a temp base tree, overlay and xdg dirs, yields a no-network `Spec` and cleans up."""
    base = tmp_path / "base"
    (base / "pkg").mkdir(parents=True)
    (base / "existing.txt").write_text("original\n")
    (base / "gone.txt").write_text("delete me\n")
    (base / "pkg" / "mod.py").write_text("X = 1\n")
    dirs = {n: tmp_path / n for n in ("upper", "work")}
    for d in dirs.values():
        d.mkdir()
    xdg = tmp_path / "xdg"
    for sub in ("config", "data", "state"):
        (xdg / sub).mkdir(parents=True)
    yield Spec(base=base, upper=dirs["upper"], work=dirs["work"], xdg=xdg, net="none")
    sandbox.remove_trial_dirs(dirs["upper"], dirs["work"])


_sh = sh


# ---------------------------------------------------------------- pure: argv construction
def test_argv_isolates_net_unless_host(spec: Spec) -> None:
    """The argv unshares the network unless the spec asks for host networking."""
    assert "--unshare-net" in sandbox.build_argv(spec, ["true"])
    host = Spec(**{**spec.__dict__, "net": "host"})
    assert "--unshare-net" not in sandbox.build_argv(host, ["true"])


def test_argv_ollama_needs_proxy_and_bridges(spec: Spec, tmp_path: Path) -> None:
    """An `ollama` net spec is refused without a proxy socket; with one the argv bridges it."""
    ollama = Spec(**{**spec.__dict__, "net": "ollama"})
    with pytest.raises(SandboxError):
        sandbox.build_argv(ollama, ["true"])
    argv = sandbox.build_argv(ollama, ["echo", "hi"], tmp_path / "s.sock")
    assert "--unshare-net" in argv
    assert any("UNIX-CONNECT:/run/ollama.sock" in a for a in argv)
    assert argv[-2:] == ["echo", "hi"]


def test_argv_clears_env_and_allowlists_root(spec: Spec) -> None:
    """The argv clears the environment, never binds the host root and mounts only the fixture."""
    argv = sandbox.build_argv(spec, ["true"])
    assert "--clearenv" in argv
    binds = [argv[i + 1] for i, a in enumerate(argv) if a == "--ro-bind"]
    assert "/" not in binds, "the host root must never be bound wholesale"
    host_paths = {a for a in argv if a.startswith("/mnt")}
    assert host_paths == {sandbox.WORKDIR}, "the fixture mount is the only /mnt path"


def test_argv_overlay_over_base(spec: Spec) -> None:
    """The argv overlays the writable upper and work dirs on the base at the workdir."""
    argv = sandbox.build_argv(spec, ["true"])
    i = argv.index("--overlay-src")
    assert argv[i + 1] == str(spec.base)
    j = argv.index("--overlay")
    assert argv[j + 1 : j + 4] == [str(spec.upper), str(spec.work), sandbox.WORKDIR]
    assert i < j


def test_the_launch_hands_bwrap_its_options_through_an_fd_not_the_argv(
    spec: Spec, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The started argv is `bwrap --args FD` and the command; the fd holds every option."""
    seen: dict[str, Any] = {}

    def fake_popen(argv: list[str], **kw: Any) -> str:
        """Record the argv and read the options fd as bwrap would."""
        (fd,) = kw["pass_fds"]
        seen.update(argv=argv, data=os.pread(fd, 1 << 20, 0))
        return "proc"

    monkeypatch.setattr(sandbox, "limit_prefix", lambda: ())
    monkeypatch.setattr(sandbox.subprocess, "Popen", fake_popen)
    probe = Spec(**{**spec.__dict__, "env": {"PRUVERA_CANARY": "zz-canary"}})
    assert sandbox._start(probe, ["echo", "hi"], None) == "proc"
    assert seen["argv"] == ["bwrap", "--args", seen["argv"][2], "echo", "hi"]
    assert seen["data"] == b"".join(a.encode() + b"\0" for a in sandbox.bwrap_options(probe))
    assert sandbox.build_argv(probe, ["echo", "hi"]) == [
        "bwrap", *sandbox.bwrap_options(probe), "echo", "hi",
    ]  # fmt: skip
    with pytest.raises(SandboxError, match="NUL"):
        sandbox._start(Spec(**{**spec.__dict__, "env": {"X": "a\0--bind"}}), ["true"], None)


def test_the_host_side_path_names_no_home_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    """bwrap's environ is readable as /proc/1/environ inside: the harness's own PATH is not it."""
    monkeypatch.setenv("PATH", f"{Path.home()}/.local/bin:/usr/bin")
    assert sandbox._host_env()["PATH"] == sandbox.HOST_PATH and "/home" not in sandbox.HOST_PATH


def test_trial_env_ignores_host_env(spec: Spec, monkeypatch: pytest.MonkeyPatch) -> None:
    """The trial environment drops host variables and sets HOME, spec variables and no bytecode."""
    monkeypatch.setenv("AGENT_TEST_CANARY_SECRET", "s3cret")
    env = sandbox.trial_env(Spec(**{**spec.__dict__, "env": {"OPENCODE_X": "1"}}))
    assert "AGENT_TEST_CANARY_SECRET" not in env
    assert env["HOME"] == sandbox.HOME and env["OPENCODE_X"] == "1"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_check_layout_rejects_bad_layouts(spec: Spec, tmp_path: Path) -> None:
    """A non-empty work dir, a missing xdg state dir or an upper equal to the base is refused."""
    sandbox.check_layout(spec)
    (spec.work / "junk").write_text("x")
    with pytest.raises(SandboxError, match="start empty"):
        sandbox.check_layout(spec)
    (spec.work / "junk").unlink()
    shutil.rmtree(spec.xdg / "state")
    with pytest.raises(SandboxError, match="xdg/state"):
        sandbox.check_layout(spec)
    same = Spec(**{**spec.__dict__, "upper": spec.base})
    with pytest.raises(SandboxError):
        sandbox.check_layout(same)


def test_tree_hash_detects_drift(spec: Spec) -> None:
    """The tree hash is stable, and a changed file or a stray cache directory counts as drift."""
    before = sandbox.tree_hash(spec.base)
    assert sandbox.tree_hash(spec.base) == before
    sandbox.verify_base(spec.base, before)
    (spec.base / "existing.txt").write_text("changed\n")
    with pytest.raises(SandboxError, match="drifted"):
        sandbox.verify_base(spec.base, before)
    (spec.base / "existing.txt").write_text("original\n")
    (spec.base / "__pycache__").mkdir()
    assert sandbox.tree_hash(spec.base) != before, "a stray cache directory must count as drift"


def test_overlay_changes_reads_upper(spec: Spec) -> None:
    """Overlay changes lists files in the upper dir as written, with none deleted or unreadable."""
    (spec.upper / "added.txt").write_text("x")
    (spec.upper / "sub").mkdir()
    (spec.upper / "sub" / "m.py").write_text("y")
    assert sandbox.overlay_changes(spec.upper) == {
        "written": ["added.txt", "sub/m.py"],
        "deleted": [],
        "unreadable": [],
    }


# ---------------------------------------------------------------- real bwrap: the escape test
@needs_bwrap
def test_writes_land_in_overlay_and_base_is_untouched(spec: Spec) -> None:
    """Creates, edits and deletes in the sandbox land in the overlay and leave the base intact."""
    before = sandbox.tree_hash(spec.base)
    result = _sh(
        spec,
        f"cd {sandbox.WORKDIR}; echo new > new.txt; echo changed > existing.txt; rm gone.txt; "
        "echo 'X = 2' > pkg/mod.py; cat existing.txt",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "changed"
    sandbox.verify_base(spec.base, before)
    changes = sandbox.overlay_changes(spec.upper)
    assert "gone.txt" in changes["deleted"]
    assert {"new.txt", "existing.txt", "pkg/mod.py"} <= set(changes["written"])


@needs_bwrap
def test_host_filesystem_is_absent(spec: Spec) -> None:
    """Host data directories, home, credentials and system paths are not visible in the sandbox."""
    home = str(Path.home())
    real = ["AIModels", "system-library", "harness", "trading", "archive", "ParamoLLC"]
    probes = [f"/mnt/ParamoStorage/{d}" for d in real]
    probes += ["/mnt/Media", "/root", "/media", home, f"{home}/.config/opencode"]
    probes += [f"{home}/.claude", "/etc/shadow", "/var/lib", f"/home/{Path.home().name}"]
    script = "for p in " + " ".join(f"'{p}'" for p in probes)
    script += '; do test -e "$p" && echo "VISIBLE:$p"; done; true'
    result = _sh(spec, script)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", result.stdout


@needs_bwrap
def test_the_real_repo_path_inside_the_sandbox_is_the_base_not_the_host_repo(spec: Spec) -> None:
    """The fixture is mounted where the real repo lives on the host: a mistake would expose it."""
    result = _sh(spec, f"ls -A {sandbox.WORKDIR} | sort | tr '\\n' ' '")
    assert result.stdout.strip() == "existing.txt gone.txt pkg", result.stdout
    host = Path(sandbox.WORKDIR)
    host_only = [
        p.name
        for p in (host.iterdir() if host.is_dir() else [])
        if p.name not in ("pkg", "existing.txt")
    ]
    if host_only:  # on this machine the host repo exists; none of its entries may show through
        names = " ".join(f"'{sandbox.WORKDIR}/{n}'" for n in host_only)
        seen = _sh(spec, f'for p in {names}; do test -e "$p" && echo VISIBLE:$p; done; true')
        assert seen.stdout.strip() == "", seen.stdout


@needs_bwrap
def test_host_canary_cannot_be_written_or_read(spec: Spec, tmp_path: Path) -> None:
    """A host file can be neither overwritten nor read from the sandbox, and stays unchanged."""
    canary = tmp_path / "host_only_canary.txt"
    canary.write_text("untouched\n")
    result = _sh(spec, f"echo pwned > '{canary}'; cat '{canary}'")
    assert result.returncode != 0
    assert canary.read_text() == "untouched\n"
    assert "untouched" not in result.stdout


@needs_bwrap
def test_system_dirs_are_read_only(spec: Spec) -> None:
    """Writes to `/usr`, `/etc/hosts` and `/etc/passwd` fail in the sandbox and leave no file."""
    script = (
        "touch /usr/x 2>/dev/null; echo usr=$?; "
        "echo x >> /etc/hosts 2>/dev/null; echo hosts=$?; "
        "echo x >> /etc/passwd 2>/dev/null; echo passwd=$?"
    )
    result = _sh(spec, script)
    for name in ("usr", "hosts", "passwd"):
        assert not re.search(rf"{name}=0\b", result.stdout), f"{name} was writable"
        assert re.search(rf"{name}=[1-9]", result.stdout), f"{name} write did not fail"
    assert not Path("/etc/x").exists() and not Path("/usr/x").exists()


@needs_bwrap
def test_ro_bind_cannot_be_written(spec: Spec, tmp_path: Path) -> None:
    """A read-only bind shows its file in the sandbox but rejects writes, leaving the host file."""
    ro = tmp_path / "ro_dir"
    ro.mkdir()
    (ro / "f.txt").write_text("keep\n")
    with_bind = Spec(**{**spec.__dict__, "ro_binds": ((ro, "/opt/ro"),)})
    result = _sh(with_bind, "cat /opt/ro/f.txt; echo x > /opt/ro/f.txt 2>/dev/null; echo w=$?")
    assert "keep" in result.stdout and re.search(r"w=[1-9]", result.stdout)
    assert (ro / "f.txt").read_text() == "keep\n"


@needs_bwrap
def test_environment_and_processes_are_isolated(
    spec: Spec, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host secret variable is absent in the sandbox, HOME is set and under 10 processes show."""
    monkeypatch.setenv("AGENT_TEST_CANARY_SECRET", "s3cret")
    result = _sh(spec, "env; echo PIDS=$(ls /proc | grep -c '^[0-9]')")
    assert "s3cret" not in result.stdout and "AGENT_TEST_CANARY_SECRET" not in result.stdout
    assert f"HOME={sandbox.HOME}" in result.stdout
    pids = int(result.stdout.rsplit("PIDS=", 1)[1].split()[0])
    assert pids < 10, "host processes must not be visible"


@needs_bwrap
@pytest.mark.parametrize("net", ["none", "ollama"])
def test_pid_1_inside_the_sandbox_shows_no_host_path_and_no_setenv_value(
    spec: Spec, tmp_path: Path, net: str
) -> None:
    """Regression (review round 9b, S5): PID 1 is bwrap's reaper, and its argv listed the whole
    launch policy (overlay, bind and data sources, the proxy socket, every `--setenv`)."""
    if net == "ollama" and shutil.which("socat") is None:
        pytest.skip("socat not installed")
    for sub in ("ro", "data"):
        (tmp_path / sub).mkdir()
    canary = "zz-setenv-canary-value"
    probe = Spec(
        **{
            **spec.__dict__,
            "net": net,
            "ro_binds": ((tmp_path / "ro", "/opt/ro"),),
            "data_base": tmp_path / "data",
            "env": {"PRUVERA_CANARY": canary},
        }
    )
    script = "tr '\\0' '\\n' </proc/1/cmdline; echo @ENV; tr '\\0' '\\n' </proc/1/environ"
    result = _sh(probe, script + "; echo @FD; ls -l /proc/1/fd 2>&1")
    assert result.returncode == 0, result.stderr
    cmdline, rest = result.stdout.split("@ENV\n", 1)
    environ, fds = rest.split("@FD\n", 1)
    argv = cmdline.splitlines()
    assert Path(argv[0]).name == "bwrap" and argv[1] == "--args", argv[:3]
    root = Path(__file__).resolve().parents[1]
    hidden = [str(p) for p in (spec.base, spec.upper, spec.work, spec.xdg, tmp_path, root)]
    for leak in [*hidden, "/home/", "ollama-proxy-", canary, "--setenv", "--ro-bind"]:
        assert leak not in cmdline, f"PID 1's argv shows {leak!r}"
    assert "/home/" not in environ and str(root) not in environ, environ
    assert "bwrap-args" not in fds, "the options fd must be closed before the sandbox starts"


@needs_bwrap
def test_python_bytecode_never_reaches_the_base(spec: Spec) -> None:
    """Importing a module in the sandbox writes no bytecode cache and leaves the base unchanged."""
    before = sandbox.tree_hash(spec.base)
    result = _sh(spec, f"cd {sandbox.WORKDIR} && python3 -c 'import pkg.mod' && ls -A pkg")
    assert result.returncode == 0, result.stderr
    assert "__pycache__" not in result.stdout
    sandbox.verify_base(spec.base, before)


_CONNECT = (
    "import socket,sys; s=socket.socket(); s.settimeout(3)\n"
    "try:\n    s.connect((sys.argv[1], int(sys.argv[2]))); print('CONNECTED')\n"
    "except OSError as e:\n    print('BLOCKED', type(e).__name__)\n"
)


def _connect(spec: Spec, host: str, port: int) -> str:
    """Runs a one-shot TCP connect probe in the sandbox and returns CONNECTED or BLOCKED output."""
    return _sh(spec, f'python3 -c "{_CONNECT}" {host} {port}').stdout


@needs_bwrap
def test_net_none_blocks_everything(spec: Spec) -> None:
    """With no network, connections to the local Ollama port and an external address are blocked."""
    assert "BLOCKED" in _connect(spec, "127.0.0.1", sandbox.OLLAMA_PORT)
    assert "BLOCKED" in _connect(spec, "1.1.1.1", 443)


@needs_bwrap
@pytest.mark.skipif(not _ollama_up(), reason="local Ollama not running")
def test_net_ollama_reaches_only_ollama(spec: Spec) -> None:
    """With Ollama net, the Ollama API answers while external and LAN addresses stay blocked."""
    ollama = Spec(**{**spec.__dict__, "net": "ollama"})
    version = _sh(
        ollama,
        'python3 -c "import urllib.request as u; '
        "print(u.urlopen('http://127.0.0.1:11434/api/version', timeout=5).read().decode())\"",
    )
    assert "version" in version.stdout, version.stderr
    assert "BLOCKED" in _connect(ollama, "1.1.1.1", 443)
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        lan_ip = probe.getsockname()[0]
    except OSError:
        lan_ip = ""
    finally:
        probe.close()
    if lan_ip and not lan_ip.startswith("127."):
        assert "BLOCKED" in _connect(ollama, lan_ip, sandbox.OLLAMA_PORT)


@needs_bwrap
def test_remove_trial_dirs_handles_unreadable_workdir(spec: Spec) -> None:
    """A work dir the kernel leaves behind after an overlay write is removed with the upper dir."""
    assert _sh(spec, f"echo a > {sandbox.WORKDIR}/a.txt").returncode == 0
    assert any(spec.work.iterdir()), "the kernel should have left its workdir behind"
    sandbox.remove_trial_dirs(spec.upper, spec.work)
    assert not spec.upper.exists() and not spec.work.exists()


def test_run_refuses_without_bwrap(spec: Spec, monkeypatch: pytest.MonkeyPatch) -> None:
    """A run with `bwrap` missing from the path is refused with a `SandboxError` naming bwrap."""
    monkeypatch.setattr(shutil, "which", lambda _name, **_kw: None)
    with pytest.raises(SandboxError, match="bwrap"):
        sandbox.run(spec, ["true"])


@needs_bwrap
@pytest.mark.skipif(not runner.OPENCODE.exists(), reason="opencode not installed")
def test_real_opencode_runs_from_a_read_only_bind(spec: Spec) -> None:
    """A read-only bound opencode binary prints its version in the sandbox and writes nothing."""
    binary = runner.OPENCODE.resolve()
    with_oc = Spec(**{**spec.__dict__, "ro_binds": ((binary, "/opt/bin/opencode"),)})
    result = _sh(with_oc, "opencode --version")
    assert re.fullmatch(r"\d+\.\d+\.\d+", result.stdout.strip()), result.stderr
    assert sandbox.overlay_changes(with_oc.upper) == {
        "written": [],
        "deleted": [],
        "unreadable": [],
    }


def test_argv_mounts_data_at_real_path_with_throwaway_writes(spec: Spec, tmp_path: Path) -> None:
    """A data base is mounted at its real path via a temporary overlay, so writes are throwaway."""
    data = tmp_path / "data"
    data.mkdir()
    with_data = Spec(**{**spec.__dict__, "data_base": data})
    argv = sandbox.build_argv(with_data, ["true"])
    i = argv.index("--tmp-overlay")
    assert argv[i - 2 : i + 2] == ["--overlay-src", str(data), "--tmp-overlay", with_data.data_dest]
    assert with_data.data_dest == "/mnt/ParamoStorage/trading"


@needs_bwrap
def test_data_slice_is_visible_at_the_real_path_and_writes_are_discarded(
    spec: Spec, tmp_path: Path
) -> None:
    """The data slice is readable at its real path, writes are discarded, only three roots exist."""
    data = tmp_path / "data"
    (data / "backtesting data").mkdir(parents=True)
    (data / "backtesting data" / "f.txt").write_text("slice\n")
    with_data = Spec(**{**spec.__dict__, "data_base": data})
    script = (
        "cat '/mnt/ParamoStorage/trading/backtesting data/f.txt'; "
        "echo new > /mnt/ParamoStorage/trading/new.txt; "
        "ls /mnt/ParamoStorage; ls /mnt/ParamoStorage/archive | wc -l"
    )
    result = _sh(with_data, script)
    listing = result.stdout.split()
    assert "slice" in listing and {"trading", "archive", "Paramo"} <= set(listing)
    assert not (data / "new.txt").exists(), "trial writes must never reach the data base"
    assert sorted(p.name for p in data.iterdir()) == ["backtesting data"]
    hidden = _sh(with_data, "ls /mnt/ParamoStorage")
    assert sorted(hidden.stdout.split()) == ["Paramo", "archive", "trading"], (
        "only the fixture, the data root and the archive exist under /mnt/ParamoStorage"
    )


_FIXTURE_VENV = Path(__file__).resolve().parents[1] / "fixtures/paramo/venv/v2"


def _tiny_wheel(dest: Path) -> Path:
    """A minimal valid wheel, so the install attempt reaches the write step, not a build step."""
    import zipfile

    wheel = dest / "zzprobe-1-py3-none-any.whl"
    info = "zzprobe-1.dist-info"
    with zipfile.ZipFile(wheel, "w") as z:
        z.writestr("zzprobe/__init__.py", "X = 1\n")
        z.writestr(f"{info}/METADATA", "Metadata-Version: 2.1\nName: zzprobe\nVersion: 1\n")
        z.writestr(
            f"{info}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: t\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        z.writestr(f"{info}/RECORD", "")
    return wheel


@needs_bwrap
@pytest.mark.skipif(not _FIXTURE_VENV.exists(), reason="fixture venv not built")
def test_pip_install_into_the_read_only_venv_fails_and_changes_nothing(
    spec: Spec, tmp_path: Path
) -> None:
    """A pip install into the read-only venv fails with a read-only error and changes nothing."""
    wheel = _tiny_wheel(tmp_path)
    binds = ((_FIXTURE_VENV, sandbox.VENV_DIR), (wheel, "/opt/zzprobe-1-py3-none-any.whl"))
    with_venv = Spec(**{**spec.__dict__, "ro_binds": binds})
    site = next((_FIXTURE_VENV / "lib").glob("python*/site-packages"))
    before = sorted(p.name for p in site.iterdir())
    wheel_path = "/opt/zzprobe-1-py3-none-any.whl"
    script = f"{sandbox.VENV_DIR}/bin/pip install --no-index {wheel_path} 2>&1 | tail -4; echo end"
    result = _sh(with_venv, script, timeout=120)
    assert re.search(r"Read-only file system|Errno 30|Permission denied", result.stdout), (
        result.stdout
    )
    assert sorted(p.name for p in site.iterdir()) == before, "the venv must be unchanged"


def test_limit_prefix_caps_memory_and_tasks_or_is_empty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The limit prefix caps memory, swap and tasks, or is empty and warns without systemd-run."""
    sandbox.limit_prefix.cache_clear()
    monkeypatch.setattr(shutil, "which", lambda _n, **_kw: None)
    assert sandbox.limit_prefix() == ()
    assert "WITHOUT" in capsys.readouterr().err, "running without caps must not be silent"
    sandbox.limit_prefix.cache_clear()
    monkeypatch.undo()
    sandbox.limit_prefix.cache_clear()
    prefix = sandbox.limit_prefix()
    sandbox.limit_prefix.cache_clear()
    if shutil.which("systemd-run") is None or sandbox._scope_prefix() == ():
        pytest.skip("no systemd user scope on this host")
    assert f"MemoryMax={sandbox.MEMORY_MAX}" in prefix and f"TasksMax={sandbox.TASKS_MAX}" in prefix
    assert "MemorySwapMax=0" in prefix


def test_the_host_side_environment_carries_only_what_systemd_run_needs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The host-side environment holds only the variables `systemd-run` needs, not secret keys."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    env = sandbox._host_env()
    assert set(env) <= {"PATH", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"}
    assert "OPENROUTER_API_KEY" not in env and env["XDG_RUNTIME_DIR"] == "/run/user/1000"


def test_upper_exceeds_counts_bytes_and_entries_and_stops_early(tmp_path: Path) -> None:
    """The size check flags too many bytes or entries, and an absent dir is under the cap."""
    (tmp_path / "d").mkdir()
    for i in range(5):
        (tmp_path / "d" / f"f{i}").write_bytes(b"x" * 100)
    assert not sandbox.upper_exceeds(tmp_path, max_bytes=1000, max_files=100)
    assert sandbox.upper_exceeds(tmp_path, max_bytes=499, max_files=100), "too many bytes"
    assert sandbox.upper_exceeds(tmp_path, max_bytes=10_000, max_files=3), "too many entries"
    assert not sandbox.upper_exceeds(tmp_path / "absent")


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every directory")
def test_a_directory_the_cap_cannot_read_counts_as_over_the_cap(tmp_path: Path) -> None:
    """Regression: `chmod 000` on a full directory hid its bytes from the walk."""
    hidden = tmp_path / "d"
    hidden.mkdir()
    (hidden / "f").write_bytes(b"x" * 10)
    hidden.chmod(0)
    try:
        assert sandbox.upper_exceeds(tmp_path)
        assert sandbox.overlay_changes(tmp_path)["unreadable"]
    finally:
        hidden.chmod(0o700)


def test_the_xdg_binds_are_measured_with_the_overlay(spec: Spec) -> None:
    """The measured roots are the overlay upper dir plus the xdg config, data and state dirs."""
    assert spec.upper in sandbox.measured_roots(spec)
    assert {p.name for p in sandbox.measured_roots(spec)[1:]} == {"config", "data", "state"}


def test_capped_output_keeps_a_bounded_amount_and_still_finishes(tmp_path: Path) -> None:
    """Capped output keeps at most the cap and finishes; a timed-out process is killed."""
    flood = "import sys; sys.stdout.write('x' * 3_000_000); sys.stderr.write('err')"
    proc = subprocess.Popen(
        [sys.executable, "-c", flood],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    out, err = sandbox._communicate_capped(proc, 30, cap=10_000)
    assert len(out) == 10_000 and err == "err" and proc.returncode == 0
    slow = subprocess.Popen(
        ["sleep", "30"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    with pytest.raises(subprocess.TimeoutExpired):
        sandbox._communicate_capped(slow, 0.3)
    assert slow.poll() is not None, "a timed-out process is killed"


@needs_bwrap
def test_a_command_that_floods_stdout_in_the_sandbox_does_not_exhaust_harness_memory(
    spec: Spec,
) -> None:
    """A command printing 80 MB in the sandbox gives at most `MAX_OUTPUT` of stdout and exits 0."""
    result = sandbox.run(spec, ["sh", "-c", "yes | head -c 80000000"], timeout=60)
    assert result.returncode == 0 and len(result.stdout) <= sandbox.MAX_OUTPUT


@needs_bwrap
def test_a_trial_cannot_create_a_nested_user_namespace(spec: Spec) -> None:
    """A command in the sandbox cannot create a nested user namespace (`unshare --user` fails)."""
    result = _sh(
        spec, "unshare --user true >/dev/null 2>&1 && echo nested-ok || echo nested-denied"
    )
    assert result.stdout.strip() == "nested-denied"


def test_fingerprint_is_the_documented_digest_of_paths_sizes_and_modes(tmp_path: Path) -> None:
    """Pins in manifests were made by the slow Path-based version; the digest must not change."""
    import hashlib

    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "x.py").write_text("hello")
    (tmp_path / "z.txt").write_text("zz")
    (tmp_path / "z.txt").chmod(0o640)
    link = tmp_path / "a" / "l"
    link.symlink_to("b/x.py")
    digest = hashlib.sha256()
    for rel in (
        "z.txt",
        "a/l",
        "a/b/x.py",
    ):  # os.walk order: a directory's files, then its subdirectories
        info = (tmp_path / rel).lstat()
        digest.update(f"{rel}\0{info.st_size}\0{info.st_mode & 0o7777}\n".encode())
    assert sandbox.fingerprint(tmp_path) == digest.hexdigest()


def test_the_ollama_bridge_needs_socat_and_a_missing_one_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing `socat` is named in a `SandboxError` for Ollama net, but not for net none."""
    real = shutil.which

    def which(name: str, *a: Any, **k: Any) -> str | None:
        """No socat, and a bwrap that is always found: the tool check runs bwrap first."""
        if name == "socat":
            return None
        return "/usr/bin/bwrap" if name == "bwrap" else real(name, *a, **k)

    monkeypatch.setattr(shutil, "which", which)
    for sub in ("base", "upper", "work", "xdg"):
        (tmp_path / sub).mkdir()
    spec = sandbox.Spec(
        base=tmp_path / "base",
        upper=tmp_path / "upper",
        work=tmp_path / "work",
        xdg=tmp_path / "xdg",
        net="ollama",
    )
    with pytest.raises(sandbox.SandboxError, match="socat not found"):
        sandbox._require_tools(spec)
    sandbox._require_tools(dataclasses.replace(spec, net="none"))
