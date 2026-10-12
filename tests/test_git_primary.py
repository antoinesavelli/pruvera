"""The opt-in `git_primary` of an experiment spec: parsing, registration, authorization, runs.

The project config declares the `git` agent a subagent, so `opencode run --agent git` falls back to
the default agent (no git prompt, no bash-only tools, no deny list), in a trial as in production.
A spec with `git_primary = true` runs the git agent as designed instead: the flag is registered and
frozen like the seed, reaches the trial config only for the `git` agent, and is recorded.

Depends on: bench.{registry,experiment,runner,preflight}, bench.issues.trials, pytest.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from bench import agentconfig, experiment, preflight, registry, runner, sandbox
from bench.issues import trials
from tests import test_runner
from tests.helpers import bwrap_works, needs_catalogue
from tests.helpers import spec as _spec

fx = test_runner.fx  # the one-commit fixture version of the runner tests (a pytest fixture)
BASE = {
    "id": "g-1",
    "question": "q",
    "kind": "exploratory",
    "baseline": "dev",
    "repeats": 3,
    "decision_rule": "r",
}


def parse(**extra: Any) -> registry.Spec:
    """Parses a spec document made of the base fields plus any extra overrides."""
    return registry.parse_spec({**BASE, **extra}, "g-1")


def test_a_spec_runs_production_unless_it_asks_for_the_git_agent() -> None:
    """The flag is off unless the spec sets it to true."""
    assert parse().git_primary is False
    assert parse(git_primary=True).git_primary is True
    assert parse(git_primary=False).git_primary is False


@pytest.mark.parametrize("bad", ["true", 1, 0, None, [True]])
def test_a_flag_that_is_not_a_boolean_is_refused_at_parse_time(bad: Any) -> None:
    """A string, a number, null or a list is not a flag: refused when the spec is parsed."""
    with pytest.raises(registry.RegistryError, match="git_primary"):
        parse(git_primary=bad)


def test_a_registration_records_the_flag(root: Path) -> None:
    """A registration stores the flag, off when absent, and the registry still verifies."""
    on = registry.register(
        _spec(root, "on-1", "exploratory", "dev", "", extra="git_primary = true\n"), root
    )
    off = registry.register(_spec(root, "off-1", "exploratory", "dev", ""), root)
    assert on["git_primary"] is True and off["git_primary"] is False
    assert registry.verify(registry.read(root)) == []


def _authorize(root: Path, name: str, git_primary: bool) -> str:
    """Authorizes a run of the named study with the given flag against a results path under root."""
    out = root / "results" / "experiments" / f"{name}.jsonl"
    return registry.authorize(name, {"b": "dev"}, None, out, root, git_primary=git_primary)


def test_a_run_must_use_the_registered_git_agent_mode(root: Path) -> None:
    """A run with another git-agent mode than the registered one is refused, both ways."""
    registry.register(
        _spec(root, "on-1", "exploratory", "dev", "", extra="git_primary = true\n"), root
    )
    registry.register(_spec(root, "off-1", "exploratory", "dev", ""), root)
    assert _authorize(root, "on-1", True) == "on-1"
    assert _authorize(root, "off-1", False) == "off-1"
    for name, wrong in (("on-1", False), ("off-1", True)):
        with pytest.raises(registry.RegistryError, match="git_primary differs"):
            _authorize(root, name, wrong)


def test_a_study_registered_before_the_flag_existed_still_runs_as_production(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row with no `git_primary` key authorizes an unflagged run and refuses a flagged one."""
    registry.register(_spec(root, "old-1", "exploratory", "dev", ""), root)
    rows = registry.read(root)
    monkeypatch.setattr(
        registry,
        "read",
        lambda _root=root: [{k: v for k, v in r.items() if k != "git_primary"} for r in rows],
    )
    assert _authorize(root, "old-1", False) == "old-1"
    with pytest.raises(registry.RegistryError, match="git_primary differs"):
        _authorize(root, "old-1", True)


def test_a_git_primary_run_without_a_registration_is_refused(root: Path) -> None:
    """A flagged run with no registered experiment is refused; an unflagged one is authorized."""
    out = root / "o.jsonl"
    with pytest.raises(registry.RegistryError, match="git-primary run needs a registered"):
        registry.authorize("", {"baseline": "dev"}, None, out, root, git_primary=True)
    assert registry.authorize("", {"baseline": "dev"}, None, out, root) == ""


def test_the_spec_template_offers_the_flag_but_does_not_set_it(root: Path) -> None:
    """The new-spec template shows a commented `git_primary` line and does not set it."""
    text = experiment.new("fresh-1", root).read_text()
    assert "# git_primary = " in text and "git_primary" not in tomllib.loads(text)


def test_a_verdict_says_when_the_git_agent_ran_as_primary() -> None:
    """A verdict notes the git agent ran as primary, and says nothing about an ordinary study."""
    row = {"kind": "confirmatory", "git_primary": True}
    assert any("git role run as a primary agent" in n for n in experiment._caveats(row, {}, []))
    assert not any(
        "primary" in n for n in experiment._caveats({**row, "git_primary": False}, {}, [])
    )
    assert not any("primary" in n for n in experiment._caveats({"kind": "confirmatory"}, {}, []))


IDS = ("mut-helpers-60", "hand-injection-comment")  # catalogue issues, as in test_orchestration


def _stand_in(name: str) -> runner.Fixture:
    """Builds a stand-in runner fixture with a missing path, an empty manifest and two issue ids."""
    manifest = {"tree_hash": "", "fixture_base_commit": ""}
    return runner.Fixture(name, Path("/nonexistent"), manifest, issue_ids=IDS)


@pytest.mark.parametrize(("extra", "expected"), [("git_primary = true\n", True), ("", False)])
def test_run_hands_the_registered_flag_to_the_campaign(
    root: Path, monkeypatch: pytest.MonkeyPatch, extra: str, expected: bool
) -> None:
    """`experiment.run` passes the registered flag on to the campaign runner."""
    registry.register(_spec(root, "e-1", "exploratory", "dev", "", extra=extra), root)
    seen: dict[str, Any] = {}
    monkeypatch.setattr(runner, "load_profile", lambda _v, p: _stand_in(p))
    monkeypatch.setattr(trials, "run_arms", lambda arms, n, out, **kw: seen.update(kw) or out)
    experiment.run("e-1", root=root)
    assert seen["git_primary"] is expected


class Recorder:
    """Stands in for runner.run_trial: remembers each trial's agent and flag."""

    def __init__(self) -> None:
        """Starts with an empty list of recorded trials."""
        self.cells: list[tuple[str, bool]] = []

    def __call__(
        self, _fx: Any, spec: runner.TrialSpec, _a: Path, _t: Path, out: Path, **_k: Any
    ) -> dict[str, Any]:
        """Records the trial's agent and flag and appends a completed row to the output."""
        self.cells.append((spec.agent, spec.git_primary))
        row = {"label": spec.label, "arm": spec.arm, "repeat": spec.repeat, "outcome": "completed"}
        with out.open("a") as fh:
            fh.write(json.dumps({**row, "experiment_id": spec.experiment_id}) + "\n")
        return row


@needs_catalogue
@pytest.mark.parametrize("flag", [True, False])
def test_every_trial_of_a_campaign_carries_the_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, flag: bool
) -> None:
    """The campaign tells authorize the flag and puts it on every trial spec."""
    rec, asked = Recorder(), []

    def authorize(experiment_id: str, *_a: Any, **kw: Any) -> str:
        """Records what authorize is asked and returns the experiment id."""
        asked.append(kw)
        return experiment_id

    monkeypatch.setattr(runner, "run_trial", rec)
    monkeypatch.setattr(preflight, "wait_clear", lambda *_a, **_k: [])
    monkeypatch.setattr(trials.registry, "authorize", authorize)
    arms = {"baseline": _stand_in("b"), "candidate": _stand_in("c")}
    trials.run_arms(arms, 1, tmp_path / "o.jsonl", experiment="e-1", git_primary=flag)
    assert asked[0]["git_primary"] is flag
    assert rec.cells and {f for _agent, f in rec.cells} == {flag}


def _assembly(agent: str) -> agentconfig.Assembly:
    """A minimal assembled config whose inline part names the agent's model, as `assemble` does."""
    inline = {"agent": {agent: {"model": "ollama/m"}}}
    return agentconfig.Assembly({}, (), "", inline)


def _env(agent: str, **kw: Any) -> dict[str, Any]:
    """The inline config a trial of `agent` would be handed, as parsed JSON."""
    spec = runner.TrialSpec(agent=agent, model="m", prompt="p", **kw)
    return json.loads(runner._config_env(_assembly(agent), spec)["OPENCODE_CONFIG_CONTENT"])


def test_the_flag_makes_the_git_agent_primary_and_keeps_its_model() -> None:
    """For the git agent the inline config sets mode primary beside the model under test."""
    assert _env("git", git_primary=True)["agent"] == {
        "git": {"model": "ollama/m", "mode": "primary"}
    }


def test_without_the_flag_the_trial_config_is_the_productions() -> None:
    """Off by default: the inline config carries no mode, so the fallback happens as before."""
    assert _env("git")["agent"] == {"git": {"model": "ollama/m"}}


def test_the_flag_changes_no_other_agent() -> None:
    """A coder or research trial of a flagged study gets the same config as an unflagged one."""
    for agent in ("coder", "research"):
        assert _env(agent, git_primary=True) == _env(agent)
    assert "mode" not in _env("coder", git_primary=True)["agent"]["coder"]


def test_an_experiments_own_inline_config_still_wins_over_the_flag() -> None:
    """The experiment's inline config is merged last, as before (here it adds an MCP server)."""
    seen = _env("git", git_primary=True, inline={"mcp": {"s": {"type": "local"}}})
    assert seen["mcp"] == {"s": {"type": "local"}} and seen["agent"]["git"]["mode"] == "primary"


needs_bwrap = pytest.mark.skipif(not bwrap_works(), reason="unprivileged bwrap unavailable")


@pytest.fixture(autouse=True)
def _clean_trial_overlays(tmp_path: Path) -> Iterator[None]:
    """Kept overlays hold a kernel workdir pytest cannot delete; reopen and remove them."""
    yield
    if (tmp_path / "trials").exists():
        sandbox.remove_trial_dirs(tmp_path / "trials")


GIT_CONFIG = {
    "model": "ollama/m",
    "provider": {"ollama": {"models": {"m": {"name": "m"}}}},
    "agent": {"git": {"model": "ollama/m"}},
}


def _trial(fx_: runner.Fixture, tmp_path: Path, **kw: Any) -> dict[str, Any]:
    """Runs one scripted git-agent trial in the sandbox that echoes its config; returns it."""
    cfg = tmp_path / "real.json"
    cfg.write_text(json.dumps(GIT_CONFIG))
    spec = runner.TrialSpec(agent="git", model="m", prompt="do it", net="none", **kw)
    return runner.run_trial(
        fx_,
        spec,
        tmp_path / "artifacts",
        tmp_path / "trials",
        tmp_path / "trials.jsonl",
        agent_argv=["sh", "-c", test_runner.SHOW_CONFIG],
        config_source=cfg,
        check=lambda force=False: [],
    )


@needs_bwrap
def test_a_flagged_trial_hands_the_agent_the_mode_and_records_the_deviation(
    fx: runner.Fixture, tmp_path: Path
) -> None:
    """The sandboxed agent sees mode primary; the record says so and names the deviation."""
    rec = _trial(fx, tmp_path, git_primary=True)
    assert rec["outcome"] == "completed" and rec["git_primary"] is True
    assert json.loads(rec["final_text"])["agent"]["git"]["mode"] == "primary"
    assert {"pointer": "/agent/git/mode", "action": "changed"}.items() <= next(
        d for d in rec["deviations"] if d["pointer"] == "/agent/git/mode"
    ).items()


@needs_bwrap
def test_an_unflagged_trial_is_production_and_says_so(fx: runner.Fixture, tmp_path: Path) -> None:
    """An unflagged git trial gets no mode, records git_primary False and no extra deviation."""
    rec = _trial(fx, tmp_path)
    assert rec["git_primary"] is False
    assert "mode" not in json.loads(rec["final_text"])["agent"]["git"]
    assert not any(d["pointer"] == "/agent/git/mode" for d in rec["deviations"])
