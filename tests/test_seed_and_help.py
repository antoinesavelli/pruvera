"""The catalogue seeder needs --write, and every documented command answers --help."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from bench import layout
from bench.issues import seed

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTED = (
    "bench.cli",
    "bench.gate",
    "bench.realism",
    "bench.issues.trials",
    "bench.issues.plant",
    "bench.issues.verify",
    "bench.issues.seed",
    "bench.fixture.pins",
    pytest.param(
        "bench.fixture.synthdata",
        marks=pytest.mark.skipif(
            importlib.util.find_spec("pandas") is None, reason="could not import pandas"
        ),
    ),
    "bench.rag.index",
)


def test_seed_without_write_is_a_dry_run_that_touches_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Seeding without `--write` exits 0, reports a dry run and never calls the seeder."""

    def boom(_root: Path) -> list[object]:
        """Stand-in seeder that raises `AssertionError` if it is called."""
        raise AssertionError("seed() must not run without --write")

    monkeypatch.setattr(seed, "seed", boom)
    assert seed.main([]) == 0
    assert "dry run" in capsys.readouterr().out


def test_seed_with_write_runs_the_seeder_and_reports_counts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """With `--write` the seeder runs and `main` exits 0, reporting how many issues were written."""
    monkeypatch.setattr(seed, "seed", lambda _root: [object(), object()])
    monkeypatch.setattr(layout, "ROOT", tmp_path)
    assert seed.main(["--write"]) == 0
    assert "2 seeded issues written" in capsys.readouterr().out


@pytest.mark.parametrize("module", DOCUMENTED)
def test_every_documented_command_answers_help(module: str) -> None:
    """Regression: `bench.gate calibrate --help` crashed on a bare `%` in its help text."""
    for argv in ([], ["calibrate"] if module == "bench.gate" else []):
        done = subprocess.run(
            [sys.executable, "-m", module, *argv, "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert done.returncode == 0, f"{module} {argv}: {done.stderr[-300:]}"
