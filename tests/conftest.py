"""Make skipped sandbox tests impossible to miss: the escape and scorer tests need bwrap + venv.

On a machine without them those tests skip and the run still reads green. This prints a loud summary
line, and `AGENT_TESTING_REQUIRE_SANDBOX=1` turns such a skip into a failed run (for a CI or a
fresh-clone check).
Depends on: pytest.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

NEEDLES = (
    "bwrap",
    "fixture venv",
    "fixture is built",
    "Ollama",
    "not installed",
    "not built",
    "not present",
    "could not import",
)


def _sandbox_skips(terminalreporter: pytest.TerminalReporter) -> list[str]:
    found = []
    for report in terminalreporter.stats.get("skipped", []):
        reason = str(report.longrepr[-1]) if isinstance(report.longrepr, tuple) else ""
        if any(n in reason for n in NEEDLES):
            found.append(report.nodeid)
    return found


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    skipped = _sandbox_skips(terminalreporter)
    if skipped:
        terminalreporter.write_sep(
            "!",
            f"{len(skipped)} TESTS SKIPPED (no sandbox, fixture or tool): those checks did NOT run",
        )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if os.environ.get("AGENT_TESTING_REQUIRE_SANDBOX") == "1" and reporter is not None:
        if _sandbox_skips(reporter) and exitstatus == 0:
            session.exitstatus = 1


@pytest.fixture(autouse=True)
def _never_write_the_real_gate_ledger(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The ledger counts real judgements; a test must never judge into it."""
    from bench import gate

    monkeypatch.setattr(gate, "LEDGER", tmp_path_factory.mktemp("ledger") / "ledger.jsonl")


@pytest.fixture(autouse=True)
def _own_session_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A test must not contend with a real campaign for the machine-wide session lock."""
    from bench import preflight

    monkeypatch.setattr(
        preflight, "SESSION_LOCK", tmp_path_factory.mktemp("lock") / ".session.lock"
    )


@pytest.fixture(autouse=True)
def _allow_uncapped_trials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake-agent tests run on hosts without a systemd user scope too."""
    monkeypatch.setenv("AGENT_TESTING_ALLOW_UNCAPPED", "1")


def _profile(root: Path, name: str, ids: list[str]) -> None:
    path = root / "issues" / "profiles" / f"{name}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'description = "{name}"\nissues = {json.dumps(ids)}\n')


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A repo with a holdout profile, a dev profile and a variant, and a clean bench/."""
    _profile(tmp_path, "holdout", ["h1", "h2"])
    _profile(tmp_path, "dev", ["d1"])
    _profile(tmp_path, "tune", ["h1", "t1"])
    (tmp_path / "variants" / "rule" / "files").mkdir(parents=True)
    (tmp_path / "variants" / "rule" / "files" / "AGENTS.md").write_text("rule v1\n")
    (tmp_path / "bench").mkdir()
    (tmp_path / "bench" / "x.py").write_text("x = 1\n")
    (tmp_path / ".gitignore").write_text("variants/\nresults/\n")
    for cmd in (
        ["init", "-q"],
        ["config", "user.email", "t@x.invalid"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", "-C", str(tmp_path), *cmd], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "base"], check=True)
    return tmp_path
