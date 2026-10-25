"""`bin/campaign.sh`: which registry states it runs, its lock, a registry refusal, a failed probe.

The harness is stubbed through the script's own hooks: `CAMPAIGN_RUNNER` runs in place of
`bench.experiment run`, `CAMPAIGN_PYTHON` answers `bench.experiment status` and the process probe,
and `AGENT_TESTING_STATE` puts the log, markers and locks in tmp_path.
Depends on: bash, flock and fuser on the host, pytest.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "campaign.sh"
FAKE_PYTHON = """#!/bin/sh
if [ "$1" = "-m" ] && [ "$3" = "status" ]; then cat "$FAKE_STATUS"; exit 0; fi
if [ "$1" = "-c" ]; then echo "probe says ${FAKE_PROBE_RC:-0}"; exit "${FAKE_PROBE_RC:-0}"; fi
exit 99
"""
RUNNER = """#!/bin/sh
echo "$1" >> "$RUNS"
if [ -n "$RUNNER_SAYS" ]; then echo "$RUNNER_SAYS"; exit 1; fi
exit 0
"""


def _exe(path: Path, text: str) -> Path:
    """Writes an executable script and returns its path."""
    path.write_text(text)
    path.chmod(0o755)
    return path


def _env(tmp_path: Path, states: dict[str, str], **extra: str) -> dict[str, str]:
    """The environment of a stubbed campaign whose registry holds `states`."""
    status = tmp_path / "status.jsonl"
    status.write_text("".join(json.dumps({"id": i, "state": s}) + "\n" for i, s in states.items()))
    return {
        "PATH": os.environ["PATH"],
        "AGENT_TESTING_STATE": str(tmp_path),
        "CAMPAIGN_PYTHON": str(_exe(tmp_path / "python", FAKE_PYTHON)),
        "CAMPAIGN_RUNNER": str(_exe(tmp_path / "runner", RUNNER)),
        "CAMPAIGN_BUSY_POLL": "1",
        "CAMPAIGN_RETRY_DELAY": "0",
        "FAKE_STATUS": str(status),
        "RUNS": str(tmp_path / "runs.txt"),
        **extra,
    }


def _campaign(
    env: dict[str, str], *ids: str, timeout: float = 30
) -> subprocess.CompletedProcess[str]:
    """Runs the campaign script on the ids with the stubbed environment."""
    return subprocess.run(
        ["bash", str(SCRIPT), *ids], env=env, capture_output=True, text=True, timeout=timeout
    )


def _runs(tmp_path: Path) -> list[str]:
    """The ids the stub runner was started with."""
    runs = tmp_path / "runs.txt"
    return runs.read_text().split() if runs.exists() else []


@pytest.mark.parametrize(
    ("state", "why"),
    [("judged", "already judged"), ("abandoned", "was abandoned"), ("", "is not registered")],
)
def test_a_judged_abandoned_or_unknown_id_is_refused_before_any_run(
    tmp_path: Path, state: str, why: str
) -> None:
    """A judged id is refused like an abandoned one: exit 64, nothing run, nothing marked done."""
    env = _env(tmp_path, {"ok-1": "started", **({"x-1": state} if state else {})})
    done = _campaign(env, "ok-1", "x-1")
    assert done.returncode == 64 and why in done.stderr
    assert _runs(tmp_path) == [] and not list((tmp_path / "artifacts").glob("*.done"))


def test_the_dry_run_shows_a_judged_id_as_refused(tmp_path: Path) -> None:
    """The dry run lists a judged id as REFUSED and exits 64, writing nothing."""
    done = _campaign(
        _env(tmp_path, {"j-1": "judged", "r-1": "registered"}), "--dry-run", "r-1", "j-1"
    )
    assert (
        done.returncode == 64 and "REFUSED (judged)" in done.stdout and "would run" in done.stdout
    )
    assert not (tmp_path / "artifacts").exists()


def test_a_registered_id_runs_until_a_pass_adds_nothing_and_is_marked_done(tmp_path: Path) -> None:
    """A run that exits 0 and adds no results finishes the id; a second start does nothing."""
    env = _env(tmp_path, {"r-1": "registered"})
    assert _campaign(env, "r-1").returncode == 0
    assert _runs(tmp_path) == ["r-1"]
    assert (tmp_path / "artifacts" / "campaign-r-1.r-1.done").exists()
    assert (tmp_path / "artifacts" / "campaign-r-1.done").exists()
    assert _campaign(env, "r-1").returncode == 0 and _runs(tmp_path) == ["r-1"]


def test_a_second_campaign_of_the_same_name_is_refused_while_one_holds_the_lock(
    tmp_path: Path,
) -> None:
    """While the campaign lock is held, another start of it exits 75 and runs nothing."""
    (tmp_path / "artifacts").mkdir()
    with (tmp_path / "artifacts" / "campaign-r-1.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        done = _campaign(_env(tmp_path, {"r-1": "registered"}), "r-1")
    assert done.returncode == 75 and "already running" in done.stderr and _runs(tmp_path) == []


def test_a_registry_refusal_is_not_retried_and_the_campaign_exits_3(tmp_path: Path) -> None:
    """A run that prints `refused:` is given up at once: one run, exit 3, no done marker."""
    env = _env(tmp_path, {"r-1": "started"}, RUNNER_SAYS="refused: r-1: already judged")
    assert _campaign(env, "r-1").returncode == 3
    assert _runs(tmp_path) == ["r-1"]
    assert (
        "refused by the registry; not retrying"
        in (tmp_path / "artifacts" / "campaign-r-1.log").read_text()
    )


def test_a_process_probe_that_fails_reads_as_busy_not_clear(tmp_path: Path) -> None:
    """A crashed bench-process probe makes the campaign wait (fail closed); no run starts."""
    env = _env(tmp_path, {"r-1": "registered"}, FAKE_PROBE_RC="1")
    with pytest.raises(subprocess.TimeoutExpired):
        _campaign(env, "r-1", timeout=4)
    assert _runs(tmp_path) == []
    assert "probe failed (exit 1)" in (tmp_path / "artifacts" / "campaign-r-1.log").read_text()
