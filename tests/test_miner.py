"""Tests for the reverted-fix miner on a synthetic repository with known fix commits."""

from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from bench import sandbox
from bench.fixture import scrub
from bench.issues import check, miner, plant, schema
from tests.test_sandbox import _bwrap_works

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_VENV = ROOT / "fixtures/paramo/venv/v2"
BUGGY = "def scale(x):\n    if x > 10:\n        return x * 2\n    return x\n"
FIXED = "def scale(x):\n    if x >= 10:\n        return x * 2\n    return x\n"
TEST = "from pkg.m import scale\n\n\ndef test_boundary():\n    assert scale(10) == 20\n"


def _git(repo: Path, *args: str) -> str:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(repo),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    done = subprocess.run(["git", "-C", str(repo), *args], env=env, capture_output=True, check=True)
    return done.stdout.decode().strip()


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    _commit(src, {"pkg/__init__.py": "", "pkg/m.py": BUGGY, "README.md": "x\n"}, "add scale")
    fix = _commit(src, {"pkg/m.py": FIXED, "tests/test_m.py": TEST}, "fix: boundary is inclusive")
    docs = _commit(src, {"README.md": "y\n"}, "fix typo in readme")  # a fix that touches no source
    big = _commit(
        src, {"pkg/big.py": "\n".join(f"x{i} = {i}" for i in range(50)) + "\n"}, "add big"
    )
    _commit(
        src, {"pkg/big.py": "\n".join(f"x{i} = {i + 1}" for i in range(50)) + "\n"}, "fix big drift"
    )
    return src, {"fix": fix, "docs": docs, "big": big}


KEPT = {"pkg/__init__.py", "pkg/m.py", "pkg/big.py", "tests/test_m.py", "README.md"}


def test_select_keeps_small_source_fixes_and_drops_the_rest(
    repo: tuple[Path, dict[str, str]],
) -> None:
    src, commits = repo
    chosen = miner.select(src, "HEAD", KEPT)
    assert [s.commit for s in chosen] == [commits["fix"]]
    assert chosen[0].sources == ("pkg/m.py",) and chosen[0].tests == ("tests/test_m.py",)
    assert miner.select(src, "HEAD", KEPT - {"pkg/m.py"}) == [], (
        "a fix in a file the fixture dropped"
    )


def test_parse_hunks_and_inverse_edits_plant_the_original_bug(
    repo: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    src, commits = repo
    sel = miner.select(src, "HEAD", KEPT)[0]
    diff = miner.source_diff(src, commits["fix"], sel.sources)
    hunks = miner.parse_hunks(diff.decode())
    assert hunks and hunks[0].after.count("x >= 10") == 1 and hunks[0].before.count("x > 10") == 1
    tree = tmp_path / "tree"
    (tree / "pkg").mkdir(parents=True)
    (tree / "pkg" / "m.py").write_text(FIXED)
    cand = miner.inverse(sel, diff, tree)
    assert isinstance(cand, miner.Candidate)
    assert plant.texts_after(tree, cand.edits)["pkg/m.py"] == BUGGY
    fixed_again = "\n".join(e.new for e in cand.edits)
    assert "x > 10" in fixed_again


def test_inverse_says_why_it_cannot_plant(
    repo: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    src, commits = repo
    sel = miner.select(src, "HEAD", KEPT)[0]
    diff = miner.source_diff(src, commits["fix"], sel.sources)
    tree = tmp_path / "moved-on"
    (tree / "pkg").mkdir(parents=True)
    (tree / "pkg" / "m.py").write_text(
        FIXED.replace("x * 2", "x * 3")
    )  # later code changed the lines
    assert "does not apply" in str(miner.inverse(sel, diff, tree))
    assert "ciphertext" in str(miner.inverse(sel, b"\x00GITCRYPT\x00abc", tree))
    created = b"diff --git a/pkg/n.py b/pkg/n.py\nnew file mode 100644\n@@ -0,0 +1 @@\n+x = 1\n"
    assert "plain modification" in str(miner.inverse(sel, created, tree))


def test_screen_drops_candidates_that_touch_tokens_or_excluded_names(tmp_path: Path) -> None:
    tokens = tmp_path / "t.txt"
    tokens.write_text("G | \\b0\\.988\\b | 0.XXX\n")
    rules = scrub.load_rules(tokens)
    clean = miner.Candidate("c", ("a.py",), (), (schema.Edit("a.py", "x = 2", "x = 1"),), 2)
    assert miner.screen(clean, rules, frozenset({"excluded_only_name"})) == []
    token = miner.Candidate("c", ("a.py",), (), (schema.Edit("a.py", "y = 0.XXX", "y = 1"),), 2)
    assert "scrub token" in miner.screen(token, rules, frozenset())[0]
    named = miner.Candidate(
        "c", ("a.py",), (), (schema.Edit("a.py", "excluded_only_name()", "x"),), 2
    )
    assert "names excluded code" in miner.screen(named, rules, frozenset({"excluded_only_name"}))[0]


def test_accept_takes_one_per_file_of_the_wanted_kind_and_to_issue_is_valid() -> None:
    def cand(i: int, file: str) -> miner.Candidate:
        return miner.Candidate(
            f"{i:040d}", (file,), ("t.py",), (schema.Edit(file, "b = 2", "b = 1"),), 2
        )

    scored = [
        (cand(1, "a.py"), miner.Verdict("caught_exception", ("t.py::x",))),
        (cand(2, "a.py"), miner.Verdict("caught_assertion", ("t.py::y",))),
        (cand(3, "a.py"), miner.Verdict("caught_assertion", ("t.py::z",))),
        (cand(4, "b.py"), miner.Verdict("caught_assertion", ("t.py::w",))),
    ]
    picked = miner.accept(scored, limit=5)
    assert [c.commit[-1] for c, _ in picked] == ["2", "4"]
    issue = miner.to_issue(*picked[0])
    assert schema.parse(tomllib.loads(schema.dumps(issue))) == issue
    assert issue.source == "reverted_fix" and issue.origin == picked[0][0].commit
    assert "became" in issue.summary or "reverted" in issue.summary


def test_kind_separates_assertion_failures_from_exceptions() -> None:
    assert miner._kind(check.Result(1, ("a",), "", {"a": "assert 1 == 2"})) == "caught_assertion"
    assert (
        miner._kind(check.Result(1, ("a",), "", {"a": "AssertionError: x"})) == "caught_assertion"
    )
    assert miner._kind(check.Result(1, ("a",), "", {"a": "NameError: y"})) == "caught_exception"
    assert miner._kind(check.Result(0)) == "survived"


@pytest.mark.skipif(
    not _bwrap_works() or not FIXTURE_VENV.exists(), reason="needs bwrap and the fixture venv"
)
def test_evaluate_classifies_a_real_planted_fix(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    (tree / "pkg").mkdir(parents=True)
    (tree / "tests").mkdir()
    (tree / "pkg" / "__init__.py").write_text("")
    (tree / "pkg" / "m.py").write_text(FIXED)
    (tree / "tests" / "test_m.py").write_text(TEST)
    env = check.Env(tree, FIXTURE_VENV)
    edits = (schema.Edit("pkg/m.py", "    if x >= 10:", "    if x > 10:"),)
    cand = miner.Candidate("c" * 40, ("pkg/m.py",), ("tests/test_m.py",), edits, 2)
    verdict = miner.evaluate(env, cand)
    assert verdict.kind == "caught_assertion" and verdict.failed == (
        "tests/test_m.py::test_boundary",
    )
    stale = miner.Candidate(
        "d" * 40, ("pkg/m.py",), ("tests/test_m.py",), (schema.Edit("pkg/m.py", "nowhere", "x"),), 2
    )
    assert miner.evaluate(env, stale).kind == "not_applicable"
    untested = miner.Candidate("e" * 40, ("pkg/m.py",), (), edits, 2)
    assert miner.evaluate(env, untested).kind == "survived"
    assert sandbox.tree_hash(tree) == sandbox.tree_hash(tree), "the tree must not change"
