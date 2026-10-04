"""Decision tiers: classification by path, the evidence each tier asks for, and verdict scope.

Depends on: bench.{gitutil,modelinfo,policy,registry}, pytest, git.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bench import gitutil, modelinfo, policy, registry

TIERS = {
    1: ["AGENTS.md", "*/AGENTS.md", "docs/agents/*.md"],
    2: ["opencode.json", "*/model-routing.yaml"],
}
SCOPE = {
    "model_digests": {"m:1b": ["d1"]},
    "opencode_version": ["1.0"],
    "fixture_source_commit": ["abc"],
}


def _row(root: Path, event: str, **fields: object) -> None:
    registry.append(root, event, **fields)


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(modelinfo, "model_digest", lambda _m: "d1")
    monkeypatch.setattr(modelinfo, "opencode_version", lambda: "1.0")
    monkeypatch.setattr(policy.layout, "source_commit", lambda *_a: "abc")


def test_a_change_is_classified_by_its_highest_file_not_by_its_message() -> None:
    assert policy.classify(["README.md", "x.py"], TIERS) == 0
    assert policy.classify(["docs/agents/GIT.md", "x.py"], TIERS) == 1
    assert policy.classify(["AGENTS.md", "machines/workstation/model-routing.yaml"], TIERS) == 2
    assert policy.classify([], TIERS) == 0


def test_the_shipped_table_puts_permissions_and_routing_in_tier_two() -> None:
    assert policy.classify(["opencode.json"]) == 2 and policy.classify(["AGENTS.md"]) == 1
    assert policy.classify(["engine/strategy/risk_manager.py"]) == 0


def test_trailers_are_read_from_a_commit_message() -> None:
    message = "feat: x\n\nbody\n\nEvidence: exp-1\nGate-Verdict: exp-2\n"
    assert policy.trailers(message) == {"Evidence": "exp-1", "Gate-Verdict": "exp-2"}


def test_tier_one_needs_a_registered_live_experiment(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(policy, "load_tiers", lambda *_a: TIERS)
    assert policy.check(["AGENTS.md"], "feat: x", root)[1] == [
        "tier 1 change: add an `Evidence: <experiment id>` trailer"
    ]
    assert "not a registered" in policy.check(["AGENTS.md"], "Evidence: nope", root)[1][0]
    _row(root, "registered", id="e1", kind="exploratory")
    assert policy.check(["AGENTS.md"], "Evidence: e1", root) == (1, [])
    _row(root, "abandoned", id="e1", reason="x")
    assert "abandoned" in policy.check(["AGENTS.md"], "Evidence: e1", root)[1][0]
    assert policy.check(["x.py"], "no trailer", root) == (0, [])


def test_tier_two_needs_a_confirmatory_clear_whose_scope_still_holds(
    root: Path, monkeypatch: pytest.MonkeyPatch, served: None
) -> None:
    monkeypatch.setattr(policy, "load_tiers", lambda *_a: TIERS)
    assert "Gate-Verdict" in policy.check(["opencode.json"], "feat", root)[1][0]
    _row(root, "registered", id="e1", kind="confirmatory")
    assert "no judged experiment" in policy.check(["opencode.json"], "Gate-Verdict: e1", root)[1][0]
    _row(root, "judged", id="e1", verdict="INCONCLUSIVE", scope=SCOPE)
    assert (
        "needs a confirmatory CLEAR"
        in policy.check(["opencode.json"], "Gate-Verdict: e1", root)[1][0]
    )
    _row(root, "registered", id="e2", kind="confirmatory")
    _row(root, "judged", id="e2", verdict="CLEAR", scope=SCOPE)
    assert policy.check(["opencode.json"], "Gate-Verdict: e2", root) == (2, [])
    _row(root, "registered", id="e3", kind="exploratory")
    _row(root, "judged", id="e3", verdict="CLEAR", scope=SCOPE)
    assert "confirmatory" in policy.clearance("e3", root)[0]


def test_a_verdict_expires_when_a_model_opencode_or_the_fixture_changes(
    monkeypatch: pytest.MonkeyPatch, served: None
) -> None:
    assert policy.scope_problems(SCOPE) == []
    monkeypatch.setattr(modelinfo, "model_digest", lambda _m: "d2")
    assert "digest is d2" in policy.scope_problems(SCOPE)[0]
    monkeypatch.setattr(modelinfo, "model_digest", lambda _m: "")
    assert "unreadable" in policy.scope_problems(SCOPE)[0]
    monkeypatch.setattr(modelinfo, "model_digest", lambda _m: "d1")
    monkeypatch.setattr(modelinfo, "opencode_version", lambda: "2.0")
    monkeypatch.setattr(policy.layout, "source_commit", lambda *_a: "zzz")
    assert len(policy.scope_problems(SCOPE)) == 2


def test_a_range_of_commits_is_checked_commit_by_commit(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(policy, "load_tiers", lambda *_a: TIERS)
    base = gitutil.text(root, "rev-parse", "HEAD")
    (root / "AGENTS.md").write_text("rule\n")
    gitutil.run(root, "add", "AGENTS.md")
    gitutil.run(root, "commit", "-qm", "rules: tighten")
    (root / "notes.md").write_text("n\n")
    gitutil.run(root, "add", "notes.md")
    gitutil.run(root, "commit", "-qm", "docs: notes")
    found = policy.check_range(root, f"{base}..HEAD", root)
    assert len(found) == 1 and "tier 1" in found[0] and "Evidence" in found[0]


def test_the_decision_row_is_text_for_the_owner_to_paste(root: Path) -> None:
    _row(root, "registered", id="e1", kind="confirmatory")
    _row(
        root,
        "judged",
        id="e1",
        verdict="CLEAR",
        diff=0.012,
        alpha_used=0.0187,
        bundle="verdicts/e1",
    )
    row = policy.decision_row("e1", root)
    assert row.startswith("| D-NNN |") and "CLEAR" in row and "verdicts/e1/REPORT.md" in row


def test_the_shipped_table_guards_the_paths_that_widen_what_a_model_may_do_unsupervised() -> None:
    for path in (
        ".claude/settings.json", ".claude/settings.local.json", ".claude/hooks/guard.py",
        ".opencode/agent/coder.md", ".mcp.json", ".githooks/pre-push",
    ):  # fmt: skip
        assert policy.classify([path]) == 2, path
    for path in (".claude/skills/x/SKILL.md", ".claude/commands/review.md", "docs/agents/GIT.md"):
        assert policy.classify([path]) == 1, path


def test_a_merge_commit_is_classified_by_what_it_brought_in(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(policy, "load_tiers", lambda *_a: TIERS)
    gitutil.run(root, "checkout", "-q", "-b", "side")
    (root / "AGENTS.md").write_text("rule\n")
    gitutil.run(root, "add", "AGENTS.md")
    gitutil.run(root, "commit", "-qm", "rules: tighten\n\nEvidence: e1")
    gitutil.run(root, "checkout", "-q", "-")
    base = gitutil.text(root, "rev-parse", "HEAD")
    gitutil.run(root, "merge", "-q", "--no-ff", "-m", "merge side", "side")
    found = policy.check_range(root, f"{base}..HEAD", root)
    assert any("merge" not in f and "tier 1" in f for f in found), found
    merge_sha = gitutil.text(root, "rev-parse", "HEAD")[:10]
    assert any(f.startswith(merge_sha) and "tier 1" in f for f in found), found
