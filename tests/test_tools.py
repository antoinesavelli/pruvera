"""Tests for the smaller tools: seeding, mining, the read audit, campaigns, venv, CLI, index."""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from bench import cli, layout, preflight, runner, sandbox
from bench.fixture import denylist, scrub
from bench.fixture import venv as fixture_venv
from bench.issues import campaign, check, mine, miner, mutate, schema, seed, verify
from bench.issues.schema import Edit

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ seed
def test_seed_code_and_the_committed_catalogue_agree_for_every_hand_issue() -> None:
    """Re-running the seeder must not silently change a committed issue (e.g. drop `protected`)."""
    catalogue = schema.load_all(ROOT / "issues")
    for hand in seed.hand_issues():
        stored = dataclasses.replace(catalogue[hand.id], proven_on="", difficulty="")
        assert dataclasses.replace(hand, proven_on="", difficulty="") == stored, hand.id


def test_mutant_issue_builds_the_planted_edit_from_a_campaign_record(tmp_path: Path) -> None:
    tree = tmp_path / "t"
    (tree / "utils").mkdir(parents=True)
    (tree / "utils" / "m.py").write_text("a = 1\nif x > 5:\n    y = 2\n")
    campaign_rec = [
        {
            "module": "utils/m.py",
            "mutants": [
                {
                    "line": 2,
                    "col": 5,
                    "old": ">",
                    "new": ">=",
                    "operator": "flip",
                    "killed": True,
                    "failed": ["tests/utils/test_m.py::test_x"],
                },
                {
                    "line": 2,
                    "col": 5,
                    "old": ">",
                    "new": "<",
                    "operator": "flip",
                    "killed": False,
                    "failed": [],
                },
            ],
        }
    ]
    seed.SUMMARIES["utils/m.py:2"] = "boundary changed"
    try:
        killed = seed.mutant_issue(tree, campaign_rec, "utils/m.py", 2, None)
        survivor = seed.mutant_issue(tree, campaign_rec, "utils/m.py", 2, "<")
    finally:
        del seed.SUMMARIES["utils/m.py:2"]
    assert killed.kind == "logic_bug_caught_by_test" and killed.detector == "test"
    assert killed.edits[0].new.count(">=") == 1 and killed.tests == (
        "tests/utils/test_m.py::test_x",
    )
    assert survivor.kind == "logic_bug_no_test_catches" and survivor.detector == "review_only"


# ------------------------------------------------------------------ mine
def test_category_maps_drop_reasons_to_categories_without_keeping_diff_text() -> None:
    assert mine.category("diff is ciphertext or binary") == "ciphertext"
    assert mine.category("does not apply: no hunk") == "does_not_apply"
    assert mine.category("scrub token in text") == "scrub_token"
    assert mine.category("surprise") == "other"


def test_kept_paths_lists_files_but_not_git(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.py").write_text("")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("")
    assert mine.kept_paths(tmp_path) == {"a/x.py"}


@pytest.fixture
def mining(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    tree = tmp_path / "tree"
    (tree / "engine").mkdir(parents=True)
    (tree / "engine" / "a.py").write_text("x = 1\n")
    monkeypatch.setattr(layout, "tree", lambda _v=layout.VERSION: tree)
    monkeypatch.setattr(mine, "OUT", tmp_path / "cands.json")
    monkeypatch.setattr(mine, "ROOT", tmp_path)
    (tmp_path / "issues").mkdir()
    monkeypatch.setattr(layout, "source_commit", lambda _v=layout.VERSION: "deadbeef")
    monkeypatch.setattr(scrub, "load_rules", lambda _p: [])
    monkeypatch.setattr(denylist, "load_rules", lambda _p: [])
    monkeypatch.setattr(miner, "excluded_identifiers", lambda *_a: frozenset())
    monkeypatch.setattr(miner, "source_diff", lambda *_a: b"")
    return tree


def _cand(commit: str, source: str) -> miner.Candidate:
    edit = Edit(source, "x = 1", "x = 2")
    return miner.Candidate(commit, (source,), ("tests/t.py",), (edit,), 2)


def test_mine_sorts_every_selected_commit_into_a_verdict_and_writes_the_list(
    mining: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sels = [miner.Selected(c, ("engine/a.py",), ("tests/t.py",), 3) for c in ("c1", "c2", "c3")]
    monkeypatch.setattr(miner, "select", lambda *_a: sels)
    outcomes = {
        "c1": "does not apply: nothing",
        "c2": _cand("c2", "engine/a.py"),
        "c3": _cand("c3", "engine/a.py"),
    }
    monkeypatch.setattr(miner, "inverse", lambda sel, *_a: outcomes[sel.commit])
    monkeypatch.setattr(
        miner, "screen", lambda cand, *_a: ["scrub token"] if cand.commit == "c3" else []
    )
    monkeypatch.setattr(miner, "evaluate", lambda *_a: miner.Verdict("caught_assertion", ("t::a",)))
    records = mine.mine()
    by_commit = {r["commit"]: r["verdict"] for r in records}
    assert by_commit == {"c1": "does_not_apply", "c3": "scrub_token", "c2": "caught_assertion"}
    assert json.loads(mine.OUT.read_text()) == records
    assert len(mine.mine(limit=1)) == 1


def test_reevaluate_and_accept_write_only_what_the_rules_allow(
    mining: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [
        {
            "commit": "c1",
            "verdict": "caught_assertion",
            "sources": ["engine/a.py"],
            "tests": ["tests/t.py"],
            "failed": ["t::a"],
            "lines": 2,
        },
        {
            "commit": "c2",
            "verdict": "caught_assertion",
            "sources": ["engine/a.py"],
            "tests": ["tests/t.py"],
            "failed": ["t::a"],
            "lines": 4,
        },
        {
            "commit": "9ffcc0b4205249c1a2d6f486eefcad417bede808",
            "verdict": "caught_assertion",
            "sources": ["engine/b.py"],
            "tests": [],
            "failed": ["t::b"],
            "lines": 1,
        },
        {
            "commit": "c4",
            "verdict": "survived",
            "sources": ["engine/c.py"],
            "tests": [],
            "failed": [],
            "lines": 1,
        },
    ]
    mine.OUT.write_text(json.dumps(rows))
    monkeypatch.setattr(miner, "inverse", lambda sel, *_a: _cand(sel.commit, sel.sources[0]))
    monkeypatch.setattr(miner, "evaluate", lambda *_a: miner.Verdict("survived", ()))
    mine.reevaluate()
    assert {r["commit"]: r["verdict"] for r in json.loads(mine.OUT.read_text())}["c1"] == "survived"
    mine.OUT.write_text(json.dumps(rows))
    written = mine.accept(10)
    assert len(written) == 1, (
        "one per file: c1 wins (fewest lines); c2 shares its file; the rejected one is skipped"
    )
    assert (mine.ROOT / "issues" / written[0] / "issue.toml").exists()
    assert mine.main(["--accept", "0"]) == 0


def test_mine_main_dispatches_to_reevaluate_accept_and_mine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def accept(n: int, verdict: str) -> list[str]:
        seen.append(f"accept{n}:{verdict}")
        return []

    def run_mine(limit: int | None = None) -> list[dict[str, object]]:
        seen.append(f"mine{limit}")
        return []

    monkeypatch.setattr(mine, "reevaluate", lambda: seen.append("re"))
    monkeypatch.setattr(mine, "accept", accept)
    monkeypatch.setattr(mine, "mine", run_mine)
    assert mine.main(["--reevaluate"]) == 0
    assert mine.main(["--accept", "3", "--verdict", "survived"]) == 0
    assert mine.main(["--limit", "2"]) == 0
    assert seen == ["re", "accept3:survived", "mine2"]


# ------------------------------------------------------------------ read audit plugin
def test_the_read_audit_logs_a_missing_data_path_once_per_test(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    code = (
        "import audit_reads as a\n"
        "a.pytest_runtest_setup(type('I', (), {'nodeid': 't1'})())\n"
        "for _ in range(2):\n"
        "    try: open('/mnt/ParamoStorage/trading/__missing__/x')\n"
        "    except OSError: pass\n"
        "try: open('/tmp/elsewhere-missing')\n"
        "except OSError: pass\n"
    )
    env = {**os.environ, "AUDIT_READS_LOG": str(log), "PYTHONPATH": str(ROOT / "bench" / "fixture")}
    subprocess.run([sys.executable, "-c", code], env=env, check=True)
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert rows == [{"test": "t1", "path": "/mnt/ParamoStorage/trading/__missing__/x"}]


# ------------------------------------------------------------------ campaign
def test_sample_is_deterministic_and_kind_balanced() -> None:
    source = "a = 1 < 2\nb = 3 > 4\nc = True\nd = [1, 2, 3, 4, 5]\ne = x[1] + 2\n"
    first = campaign.sample(source, 7, per_kind=1)
    assert first == campaign.sample(source, 7, per_kind=1)
    kinds = [m.operator for m in first]
    assert len(kinds) == len(set(kinds)), "at most one mutant per kind"
    assert campaign.sample(source, 7, per_kind=50) == sorted(
        mutate.candidates(source), key=lambda m: (m.line, m.col)
    )


def test_evaluate_marks_mutants_killed_or_survived_and_skips_a_red_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = tmp_path / "t"
    (tree / "utils").mkdir(parents=True)
    (tree / "utils" / "m.py").write_text("ok = 1 < 2\n")
    env = check.Env(tree, tmp_path / "venv")

    def fake(
        _env: Any, _args: Any, overrides: dict[str, str] | None = None, **_k: Any
    ) -> check.Result:
        if overrides is None:
            return check.Result(0)
        return (
            check.Result(1, ("t::x",))
            if "<=" in next(iter(overrides.values()))
            else check.Result(0)
        )

    monkeypatch.setattr(check, "run_pytest", fake)
    out = campaign.evaluate(env, "utils/m.py", "tests/utils/test_m.py")
    assert out["baseline_passed"] and {m["killed"] for m in out["mutants"]} <= {True, False}
    monkeypatch.setattr(check, "run_pytest", lambda *_a, **_k: check.Result(1, ("t::x",)))
    assert campaign.evaluate(env, "utils/m.py", "tests/utils/test_m.py")["mutants"] == []


def test_campaign_main_writes_one_record_per_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(layout, "ROOT", tmp_path)
    monkeypatch.setattr(
        campaign, "evaluate", lambda env, m, t, seed=1: {"module": m, "mutants": [{"killed": True}]}
    )
    assert campaign.main(["utils/a.py", "utils/b.py"]) == 0
    written = json.loads((tmp_path / "issues" / "_campaign.json").read_text())
    assert [r["module"] for r in written] == ["utils/a.py", "utils/b.py"]
    assert "1/1 killed" in capsys.readouterr().out


# ------------------------------------------------------------------ venv
def test_shebangs_point_at_the_trial_path_and_the_pth_puts_the_tree_on_the_path(
    tmp_path: Path,
) -> None:
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "lib" / "python3.12" / "site-packages").mkdir(parents=True)
    host = f"#!{venv.resolve()}/bin/python\nprint(1)\n"
    (venv / "bin" / "tool").write_text(host)
    (venv / "bin" / "old").write_text("#!/venv/bin/python\nprint(2)\n")
    (venv / "bin" / "binary").write_bytes(b"\x7fELF")
    (venv / "bin" / "link").symlink_to("tool")
    changed = fixture_venv.rewrite_shebangs(venv)
    assert changed == ["old", "tool"]
    assert (venv / "bin" / "tool").read_text().startswith(f"#!{sandbox.VENV_DIR}/bin/python\n")
    assert fixture_venv.rewrite_shebangs(venv) == [], "idempotent"
    pth = fixture_venv.add_tree_pth(venv)
    assert pth.read_text() == sandbox.WORKDIR + "\n"


def test_venv_build_refuses_without_uv_or_over_an_existing_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _n: None)
    with pytest.raises(fixture_venv.VenvError, match="uv not found"):
        fixture_venv.build(tmp_path, tmp_path / "v")
    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/uv")
    (tmp_path / "v").mkdir()
    with pytest.raises(fixture_venv.VenvError, match="already exists"):
        fixture_venv.build(tmp_path, tmp_path / "v")


def test_venv_build_runs_each_step_and_reports_a_failing_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/uv")
    ran: list[list[str]] = []

    def fake_run(cmd: list[str], **_k: Any) -> subprocess.CompletedProcess[str]:
        ran.append(cmd)
        return subprocess.CompletedProcess(cmd, 1 if len(ran) == 2 else 0, "", "boom")

    monkeypatch.setattr(subprocess, "run", fake_run)
    (tmp_path / "requirements-nodeps.txt").write_text("")
    with pytest.raises(fixture_venv.VenvError, match="boom"):
        fixture_venv.build(tmp_path, tmp_path / "new")
    assert len(ran) == 2 and ran[0][1] == "venv"


# ------------------------------------------------------------------ cli
def test_cli_check_reports_the_fixture_and_the_preflight_and_trial_runs_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fx = runner.Fixture("v", Path("/x"), {"tree_hash": "", "fixture_base_commit": ""})
    monkeypatch.setattr(cli, "load", lambda _v, _p: fx)
    monkeypatch.setattr(runner, "check_fixture", lambda _fx: None)
    monkeypatch.setattr(preflight, "problems", lambda: [])
    assert cli.main(["check"]) == 0 and '"fixture": "ok"' in capsys.readouterr().out
    monkeypatch.setattr(preflight, "problems", lambda: [preflight.Problem("gpu_busy", "busy")])
    assert cli.main(["check"]) == 2
    capsys.readouterr()
    seen: dict[str, Any] = {}

    def fake_trial(_fx: Any, spec: runner.TrialSpec, *_a: Any, **kw: Any) -> dict[str, Any]:
        seen.update(spec=spec, force=kw.get("force"))
        return {
            "trial_id": "t",
            "outcome": "completed",
            "secs": 1,
            "tool_calls": 0,
            "artifact": "a",
        }

    monkeypatch.setattr(runner, "run_trial", fake_trial)
    monkeypatch.setattr(preflight, "wait_clear", lambda *_a, **_k: [])
    code = cli.main(
        [
            "trial",
            "--agent",
            "research",
            "--model",
            "m",
            "--prompt",
            "hi",
            "--hook",
            "dirty:a.py:x",
            "--force",
            "--wait",
            "5",
        ]
    )
    assert code == 0 and seen["force"] is True and seen["spec"].hooks[0].path == "a.py"
    assert cli.parse_hook("untracked:b.txt").content == "# seeded by the trial\n"
    monkeypatch.setattr(runner, "reproduce_diff", lambda _fx, d: f"diff of {d.name}\n")
    assert cli.main(["replay", "/x/overlays/abc"]) == 0
    assert capsys.readouterr().out.endswith("diff of abc\n")


# ------------------------------------------------------------------ verify main
def test_verify_main_stamps_proofs_only_for_issues_that_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    good = Edit("p/m.py", "a", "b")
    issues = {
        "x-1": schema.Issue(
            "x-1", "doc_drift", "hand", "easy", (), "s", "review_only", (), "fix", (good,)
        ),
        "x-2": schema.Issue(
            "x-2", "doc_drift", "hand", "easy", (), "s", "review_only", (), "fix", (good,)
        ),
    }
    root = tmp_path / "root"
    (root / "issues").mkdir(parents=True)
    monkeypatch.setattr(verify, "__file__", str(root / "bench" / "issues" / "verify.py"))
    monkeypatch.setattr(schema, "load_all", lambda _p: issues)
    fx = runner.Fixture("v2", Path("/x"), {}, venv=Path("/v"), issue_ids=("x-1",))
    monkeypatch.setattr(cli, "load", lambda _v, _p: fx)
    monkeypatch.setattr(
        verify,
        "verify_issue",
        lambda _e, i: verify.Verdict(i.id, i.id == "x-1", [] if i.id == "x-1" else ["bad"]),
    )
    monkeypatch.setattr(
        verify, "verify_profile", lambda *_a: {"ok": True, "red_set": [], "issues": []}
    )
    assert verify.main(["--write"]) == 1, "x-2 fails, so the run fails"
    out = capsys.readouterr().out
    assert "ok   x-1" in out and "FAIL x-2" in out
    stamped = schema.load(root / "issues" / "x-1" / "issue.toml")
    assert stamped.proven_on == "v2" and not (root / "issues" / "x-2").exists()
    assert verify.main(["--profile", "p"]) == 1 and "ok" in capsys.readouterr().out


# ------------------------------------------------------------------ rag index
def test_the_index_chunks_docs_embeds_them_and_writes_the_arrays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    np = pytest.importorskip("numpy")
    from bench.rag import index

    tree = tmp_path / "tree"
    (tree / "docs" / "eval").mkdir(parents=True)
    (tree / "docs" / "a.md").write_text("a" * 3200)
    (tree / "docs" / "eval" / "key.md").write_text("answer key")
    (tree / "x.py").write_text("code")
    chunks = index.chunks(tree)
    assert [c["file"] for c in chunks] == ["docs/a.md"] * 3, (
        "3 chunks; eval answer key and code skipped"
    )
    assert all(c["text"].startswith("docs/a.md\n") for c in chunks)

    class Resp:
        def __init__(self, n: int) -> None:
            self.n = n

        def __enter__(self) -> Resp:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps({"embeddings": [[3.0, 4.0]] * self.n}).encode()

    def fake_open(req: Any, timeout: int = 0) -> Resp:
        return Resp(len(json.loads(req.data)["input"]))

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    vectors = index.embed_documents(["a", "b"])
    assert vectors.shape == (2, 2) and abs(float(np.linalg.norm(vectors[0])) - 1.0) < 1e-6
    out = tmp_path / "idx"
    assert index.build(tree, out) == {"chunks": 3, "files": 1}
    assert json.loads((out / "chunks.json").read_text())[0]["file"] == "docs/a.md"
    assert np.load(out / "vectors.npy").shape == (3, 2)


def test_reseeding_keeps_a_measured_difficulty_and_proof(tmp_path: Path) -> None:
    catalogue = tmp_path / "issues"
    hand = seed.hand_issues()[0]
    schema.write(catalogue, dataclasses.replace(hand, difficulty="hard", proven_on="v2"))
    written = seed.write_preserving(catalogue, [hand])
    assert written[0].difficulty == "hard" and written[0].proven_on == "v2"
    assert schema.load(catalogue / hand.id / "issue.toml").difficulty == "hard"
    fresh = seed.write_preserving(tmp_path / "other", [hand])
    assert fresh[0].difficulty == hand.difficulty and fresh[0].proven_on == ""
