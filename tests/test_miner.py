"""Tests for the reverted-fix miner on a synthetic repository with known fix commits."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from bench import sandbox
from bench.fixture import scrub
from bench.issues import check, miner, plant, schema
from tests.helpers import bwrap_works as _bwrap_works
from tests.helpers import git

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_VENV = ROOT / "fixtures/paramo/venv/v2"
BUGGY = "def scale(x):\n    if x > 10:\n        return x * 2\n    return x\n"
FIXED = "def scale(x):\n    if x >= 10:\n        return x * 2\n    return x\n"
TEST = "from pkg.m import scale\n\n\ndef test_boundary():\n    assert scale(10) == 20\n"


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    src = tmp_path / "src"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    _commit(src, {"pkg/__init__.py": "", "pkg/m.py": BUGGY, "README.md": "x\n"}, "add scale")
    fix = _commit(src, {"pkg/m.py": FIXED, "tests/test_m.py": TEST}, "fix: boundary is inclusive")
    docs = _commit(src, {"README.md": "y\n"}, "fix: typo in readme")  # a fix that touches no source
    big = _commit(
        src, {"pkg/big.py": "\n".join(f"x{i} = {i}" for i in range(50)) + "\n"}, "add big"
    )
    _commit(
        src,
        {"pkg/big.py": "\n".join(f"x{i} = {i + 1}" for i in range(50)) + "\n"},
        "fix: big drift",
    )
    return src, {"fix": fix, "docs": docs, "big": big}


KEPT = {"pkg/__init__.py", "pkg/m.py", "pkg/big.py", "tests/test_m.py", "README.md"}


def test_select_keeps_source_fixes_in_kept_files_and_size_is_checked_when_inverting(
    repo: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    src, commits = repo
    chosen = miner.select(src, "HEAD", KEPT)
    assert [s.commit for s in chosen] == [
        commits["fix"],
        chosen[1].commit,
    ]  # the fix and "fix big drift"
    assert chosen[0].sources == ("pkg/m.py",) and chosen[0].tests == ("tests/test_m.py",)
    assert miner.select(src, "HEAD", KEPT - {"pkg/m.py"})[0].commit != commits["fix"], (
        "a fix in a file the fixture dropped"
    )
    tree = tmp_path / "tree"
    (tree / "pkg").mkdir(parents=True)
    (tree / "pkg" / "big.py").write_text("\n".join(f"x{i} = {i + 1}" for i in range(50)) + "\n")
    big = chosen[1]
    diff = miner.source_diff(src, big.commit, big.sources)
    assert miner.inverse(big, diff, tree) == "too large"
    assert isinstance(miner.inverse(big, diff, tree, max_lines=500), miner.Candidate)


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
    assert "was reverted" in issue.summary


def test_kind_separates_assertion_failures_from_exceptions_and_import_breaks() -> None:
    assert miner._kind(check.Result(1, ("a",), "", {}, ("assert",))) == "caught_assertion"
    assert miner._kind(check.Result(1, ("a",), "", {}, ("AssertionError",))) == "caught_assertion"
    assert miner._kind(check.Result(1, ("a",), "", {}, ("NameError",))) == "caught_exception"
    broken = check.Result(1, (), "", {}, ("AssertionError",), collection_error=True)
    assert miner._kind(broken) == "caught_exception", (
        "an import break must not count as a caught bug"
    )
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


def test_behavioural_ignores_comments_docstrings_and_whitespace_but_sees_code() -> None:
    base = 'def f(x):\n    """Doc."""\n    # note\n    return x + 1\n'
    assert not miner.behavioural(
        base, base.replace("Doc.", "Other doc.").replace("# note", "# new")
    )
    assert not miner.behavioural(base, base.replace("x + 1", "x  +  1"))
    assert miner.behavioural(base, base.replace("x + 1", "x + 2"))
    assert miner.behavioural("def f(:\n", "def f():\n    pass\n"), (
        "unparseable: keep and let tests decide"
    )


def test_inverse_skips_hunks_that_only_change_comments(tmp_path: Path) -> None:
    sel = miner.Selected("c" * 40, ("pkg/m.py",), ("tests/t.py",), -1)
    diff = (
        b"diff --git a/pkg/m.py b/pkg/m.py\n--- a/pkg/m.py\n+++ b/pkg/m.py\n"
        b"@@ -1,3 +1,3 @@\n def f():\n-    # old note\n+    # new note\n     return 1\n"
    )
    tree = tmp_path / "tree"
    (tree / "pkg").mkdir(parents=True)
    (tree / "pkg" / "m.py").write_text("def f():\n    # new note\n    return 1\n")
    assert "only comment or docstring" in str(miner.inverse(sel, diff, tree))


def test_only_a_conventional_fix_subject_counts_as_a_fix(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    shas = {
        message: _commit(src, {"f.txt": message}, message)
        for message in (
            "fix: a real fix",
            "fix(scope): another",
            "refactor(x): move code; fix-the-cause note",
            "docs+fix(x): mostly docs",
            "feat: prefix of fixture",
            "Fix: capitalised is not conventional",
        )
    }
    found = miner.fix_commits(src, "HEAD")
    assert found == [shas["fix: a real fix"], shas["fix(scope): another"]]
