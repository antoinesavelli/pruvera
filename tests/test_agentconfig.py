"""Tests for bench.agentconfig: assembly, deviations, parity, and opencode's resolved view."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from bench import agentconfig, sandbox
from bench.agentconfig import ParityError
from tests.helpers import bwrap_works as _bwrap_works
from tests.helpers import sh as _sh

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures/paramo"
OPENCODE = Path.home() / ".opencode" / "bin" / "opencode"

REAL = {
    "model": "ollama/a",
    "provider": {
        "ollama": {"options": {"baseURL": "http://localhost:11434/v1"}},
        "openrouter": {"options": {"apiKey": "{env:K}"}},
    },
    "mcp": {"m": {"type": "local"}},
    "agent": {"plan": {"model": "openrouter/x"}, "git": {"model": "ollama/a"}},
}


def _write_real(tmp_path: Path) -> Path:
    path = tmp_path / "opencode.json"
    path.write_text(json.dumps(REAL))
    return path


def test_diff_pointers() -> None:
    other = {"a": 1, "b": {"c": 2}, "d": 4}
    real = {"a": 1, "b": {"c": 3}, "e": 5}
    assert agentconfig.diff_pointers(real, other) == [
        ("/b/c", "changed"),
        ("/d", "added"),
        ("/e", "removed"),
    ]


def test_assemble_removes_remote_parts_and_records_every_change(tmp_path: Path) -> None:
    asm = agentconfig.assemble("git", "gpt-oss:20b", _write_real(tmp_path))
    assert "openrouter" not in asm.config["provider"] and "mcp" not in asm.config
    assert asm.config["agent"]["plan"]["model"] == "ollama/gpt-oss:20b"
    assert asm.inline == {"agent": {"git": {"model": "ollama/gpt-oss:20b"}}}
    assert json.loads(asm.env()["OPENCODE_CONFIG_CONTENT"]) == asm.inline
    assert {d.pointer for d in asm.deviations} == {
        "/provider/openrouter",
        "/mcp",
        "/agent/plan/model",
    }
    agentconfig.check_parity(REAL, asm.config, asm.deviations)
    assert REAL["mcp"], "the real config object must not be mutated"


def test_parity_fails_on_an_unexplained_difference(tmp_path: Path) -> None:
    asm = agentconfig.assemble("git", "gpt-oss:20b", _write_real(tmp_path))
    tampered = json.loads(json.dumps(asm.config))
    tampered["agent"]["git"]["model"] = "ollama/other"
    with pytest.raises(ParityError, match="/agent/git/model"):
        agentconfig.check_parity(REAL, tampered, asm.deviations)
    with pytest.raises(ParityError):
        agentconfig.check_parity(REAL, asm.config, ())


def test_deviations_toml_matches_the_code(tmp_path: Path) -> None:
    doc = tomllib.loads((FIXTURE / "deviations.toml").read_text())
    listed = " ".join(d["how"] for d in doc["deviation"])
    asm = agentconfig.assemble("git", "gpt-oss:20b", _write_real(tmp_path))
    for dev in asm.deviations:
        head = "/agent/<name>/model" if dev.pointer.startswith("/agent/") else dev.pointer
        assert head in listed, f"{dev.pointer} is a deviation in code but not in deviations.toml"


def test_write_puts_the_global_config_under_xdg(tmp_path: Path) -> None:
    asm = agentconfig.assemble("git", "m", _write_real(tmp_path))
    target = agentconfig.write(asm, tmp_path / "config")
    assert target == tmp_path / "config/opencode/opencode.json"
    assert json.loads(target.read_text()) == asm.config


def _real_available() -> bool:
    return (
        agentconfig.REAL_GLOBAL.exists()
        and OPENCODE.exists()
        and (ROOT / "fixtures/paramo/venv/v2").exists()
        and (ROOT / "fixtures/paramo/versions/v2/tree").exists()
    )


@pytest.mark.skipif(
    not _bwrap_works() or not _real_available(), reason="needs bwrap and the fixture"
)
def test_opencode_resolves_the_trial_config_as_designed(tmp_path: Path) -> None:
    base = ROOT / "fixtures/paramo/versions/v2/tree"
    dirs = {n: tmp_path / n for n in ("upper", "work")}
    for d in dirs.values():
        d.mkdir()
    for sub in ("config", "data", "state"):
        (tmp_path / "xdg" / sub).mkdir(parents=True)
    asm = agentconfig.assemble("git", "gpt-oss:20b")
    agentconfig.write(asm, tmp_path / "xdg" / "config")
    spec = sandbox.Spec(
        base=base,
        upper=dirs["upper"],
        work=dirs["work"],
        xdg=tmp_path / "xdg",
        net="none",
        ro_binds=((OPENCODE.resolve(), "/opt/bin/opencode"),),
        env={"PATH": "/opt/bin:/usr/bin:/bin", **asm.env()},
    )
    try:
        # Written to a file first: opencode truncates large output when stdout is a pipe.
        cmd = "opencode debug config >/tmp/r.json 2>/dev/null; cat /tmp/r.json"
        script = f"cd {sandbox.WORKDIR} && {cmd}"
        out = _sh(spec, script, timeout=120).stdout
        resolved = json.loads(out)
    finally:
        sandbox.remove_trial_dirs(dirs["upper"], dirs["work"])
    assert sorted(resolved["provider"]) == ["ollama"] and not resolved.get("mcp")
    assert resolved["agent"]["git"]["model"] == "ollama/gpt-oss:20b"
    assert resolved["agent"]["plan"]["model"] == "ollama/gpt-oss:20b"
    assert "git agent for Paramo" in resolved["agent"]["git"]["prompt"], "the real prompt loads"
    options = [p.get("options", {}) for p in resolved["provider"].values()]
    assert not any("apiKey" in o for o in options), "no provider may carry an API key"
