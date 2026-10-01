"""Tests for bench.preflight against a fake /proc tree, so no real process or GPU is involved."""

from __future__ import annotations

from pathlib import Path

import pytest

from bench import preflight
from bench.preflight import PreflightError


def _proc(root: Path, pid: int, argv: list[str], ppid: int = 1) -> None:
    d = root / str(pid)
    d.mkdir()
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (d / "stat").write_text(f"{pid} (x) S {ppid} 0 0\n")


@pytest.fixture
def proc(tmp_path: Path) -> Path:
    root = tmp_path / "proc"
    root.mkdir()
    _proc(root, 10, ["/usr/bin/bash", "-c", "sleep 1"])
    return root


def test_quiet_host_has_no_problems(proc: Path) -> None:
    assert preflight.problems(proc, gpu=lambda: 3, ollama=lambda: True, me=999) == []


def test_running_opencode_is_a_problem(proc: Path) -> None:
    _proc(proc, 20, ["/home/u/.opencode/bin/opencode", "run", "--agent", "x"])
    found = preflight.running_agents(proc, me=999)
    assert [p.code for p in found] == ["agent_running"] and "pid 20" in found[0].message


def test_bench_scripts_match_by_argv_not_substring(proc: Path) -> None:
    _proc(proc, 21, ["python3", "bench/nav_bench.py", "run", "--n", "3"])
    _proc(proc, 22, ["/bin/bash", "-c", "echo opencode nav_bench.py bench.py"])
    found = preflight.running_agents(proc, me=999)
    assert [p.message for p in found] == ["pid 21: nav_bench.py is running"]


def test_own_process_and_parents_are_ignored(proc: Path) -> None:
    _proc(proc, 30, ["python3", "bench/bench.py", "run"], ppid=1)
    _proc(proc, 31, ["/x/opencode", "run"], ppid=30)
    assert preflight.running_agents(proc, me=31) == []
    assert len(preflight.running_agents(proc, me=999)) == 2


def test_busy_gpu_and_missing_ollama(proc: Path) -> None:
    found = preflight.problems(proc, gpu=lambda: 85, ollama=lambda: False, me=999)
    assert {p.code for p in found} == {"gpu_busy", "ollama_down"}


def test_an_unreadable_gpu_blocks_because_contention_is_then_unknown(proc: Path) -> None:
    found = preflight.problems(proc, gpu=lambda: None, ollama=lambda: True, me=999)
    assert [p.code for p in found] == ["gpu_unreadable"]


def test_a_python_dash_m_harness_entry_point_counts_as_a_running_bench(proc: Path) -> None:
    _proc(proc, 20, ["python3", "-m", "bench.realism", "--n", "3"])
    _proc(proc, 21, ["python3", "-m", "pytest", "tests"])
    found = preflight.running_agents(proc, me=999)
    assert [(p.code, "bench.realism" in p.message) for p in found] == [("bench_running", True)]


def test_check_raises_unless_forced(proc: Path) -> None:
    _proc(proc, 40, ["/x/opencode", "run"])
    with pytest.raises(PreflightError, match="agent_running"):
        preflight.check(proc_root=proc, gpu=lambda: 1, ollama=lambda: True, me=999)
    forced = preflight.check(True, proc, lambda: 1, lambda: True, 999)
    assert [p.code for p in forced] == ["agent_running"]


def test_real_host_scan_runs_without_error() -> None:
    assert isinstance(preflight.running_agents(), list)
    util = preflight.gpu_utilization()
    assert util is None or 0 <= util <= 100


def test_wait_clear_polls_until_clear_or_timeout(proc: Path) -> None:
    readings = iter([90, 60, 5])
    now = [0.0]

    def clock() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    found = preflight.wait_clear(
        30,
        2,
        sleep=sleep,
        clock=clock,
        proc_root=proc,
        gpu=lambda: next(readings),
        ollama=lambda: True,
        me=999,
    )
    assert found == [] and now[0] == 4.0
    stuck = preflight.wait_clear(
        10, 2, sleep=sleep, clock=clock, proc_root=proc, gpu=lambda: 99, ollama=lambda: True, me=999
    )
    assert [p.code for p in stuck] == ["gpu_busy"]


def test_a_second_run_of_trials_cannot_take_the_session_lock(tmp_path: Path) -> None:
    lock = tmp_path / "o" / ".session.lock"
    with preflight.session_lock(lock):
        with pytest.raises(PreflightError, match="another run"):
            with preflight.session_lock(lock):
                pass
    with preflight.session_lock(lock):  # released on exit
        pass


def test_every_harness_entry_point_that_starts_trials_is_recognised(proc: Path) -> None:
    for i, module in enumerate(("bench.gate", "bench.issues.trials", "bench.cli")):
        _proc(proc, 30 + i, ["python3", "-m", module, "run"])
    codes = [p.message for p in preflight.running_agents(proc, me=999)]
    assert len(codes) == 3
