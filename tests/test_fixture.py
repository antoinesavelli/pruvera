"""Tests for bench.fixture on a synthetic repo: no real Paramo content is read or written."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from bench.fixture import build, denylist, export, scrub, verify


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


FILES = {
    "README.md": "Area map.\nThe tuned cell gave DSR 0.XXX in-sample for private_strategy.\n",
    "config/keep.py": "X = 1\n# private_strategy fit gave win_loss 0.XXX\nY = 0.XXX\n",
    "config/strategies/private_strategy.py": "SECRET_RECIPE_KNOB = 0.60\n",
    "engine/source_secret.py": "class RecipeSource:\n    LONG_RECIPE_NAME = 5\n",
    "docs/research/finding.md": "the finding\n",
    "scripts/analysis/private_strategy/run.py": "def run_recipe_scan(): ...\n",
    "tests/test_uses_recipe.py": "from scripts.analysis.private_strategy import run\n",
    "tests/test_keeps.py": "from config import keep\n\ndef test_x():\n    assert keep.X == 1\n",
    "data/big.parquet": "not real data\n",
}
DENY = (
    "docs/research/**\n**/*private_strategy*\nscripts/analysis/private_strategy/**\n"
    "engine/source_secret.py\ndata/**\n"
)
TOKENS = "G | \\b0\\.988\\b | 0.XXX\nG | \\b0\\.6737\\b | 0.XXXX\n"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    for rel, text in FILES.items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(text)
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "base")
    return src


# ---------------------------------------------------------------- denylist
def test_glob_semantics() -> None:
    rules = ["docs/research/**", "**/*private_strategy*", "data/**", "*.key", "a/?/b.txt"]
    paths = [
        "docs/research/x/y.md",
        "docs/other.md",
        "config/strategies/private_strategy.py",
        "private_strategy_top.py",
        "data/a/b.parquet",
        "secret.key",
        "sub/secret.key",
        "a/1/b.txt",
        "a/12/b.txt",
    ]
    assert denylist.excluded(paths, rules) == {
        "docs/research/x/y.md",
        "config/strategies/private_strategy.py",
        "private_strategy_top.py",
        "data/a/b.parquet",
        "secret.key",
        "a/1/b.txt",
    }


def test_load_rules_strips_comments(tmp_path: Path) -> None:
    f = tmp_path / "d.txt"
    f.write_text("# header\n\ndocs/**  # trailing note\nplain\n  # indented comment\n")
    assert denylist.load_rules(f) == ["docs/**", "plain"]


def test_module_names_and_dependent_tests(tmp_path: Path) -> None:
    assert denylist.module_name("pkg/sub/__init__.py") == "pkg.sub"
    assert denylist.module_name("pkg/mod.py") == "pkg.mod"
    assert denylist.module_name("README.md") is None
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("from pkg.gone import thing\n")
    (tmp_path / "tests" / "test_b.py").write_text("import pkg.gone.deep\n")
    (tmp_path / "tests" / "test_c.py").write_text("from pkg import gone\n")
    (tmp_path / "tests" / "test_d.py").write_text("from pkg import stays\n")
    paths = [f"tests/test_{c}.py" for c in "abcd"]
    assert denylist.dependent_tests(tmp_path, paths, {"pkg.gone"}) == {
        "tests/test_a.py",
        "tests/test_b.py",
        "tests/test_c.py",
    }


# ---------------------------------------------------------------- export
def test_export_uses_private_index_and_leaves_source_untouched(repo: Path, tmp_path: Path) -> None:
    (repo / "wip.txt").write_text("uncommitted peer work\n")
    _git(repo, "add", "wip.txt")
    staged_before = _git(repo, "diff", "--cached", "--name-only")
    commit = export.resolve(repo, "HEAD")
    paths = export.export_commit(repo, commit, tmp_path / "out")
    assert "wip.txt" not in paths and not (tmp_path / "out" / "wip.txt").exists()
    assert _git(repo, "diff", "--cached", "--name-only") == staged_before == "wip.txt"
    assert (tmp_path / "out" / "README.md").read_text() == FILES["README.md"]


def test_export_refuses_nonempty_destination(repo: Path, tmp_path: Path) -> None:
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "x").write_text("x")
    with pytest.raises(export.ExportError):
        export.export_commit(repo, export.resolve(repo, "HEAD"), dest)


def test_ciphertext_detection(tmp_path: Path) -> None:
    (tmp_path / "ok.txt").write_text("plain")
    (tmp_path / "bad.bin").write_bytes(export.CIPHERTEXT_HEADER + b"\x01\x02")
    assert export.ciphertext_files(tmp_path) == ["bad.bin"]


# ---------------------------------------------------------------- scrub
def _rules(tmp_path: Path, text: str) -> list[scrub.Rule]:
    f = tmp_path / "tokens.txt"
    f.write_text(text)
    return scrub.load_rules(f)


def test_global_and_contextual_rules(tmp_path: Path) -> None:
    rules = _rules(
        tmp_path,
        "G | \\b0\\.988\\b | 0.XXX\nC | rolled | (=|>=)\\s*0\\.10\\b | \\1 0.XX\n",
    )
    out = scrub.scrub_text("DSR 0.XXX\nunrelated rolled >= 0.10 here\n", rules, is_python=False)
    assert "0.XXX" not in out.text
    assert "rolled >= 0.10" in out.text, "contextual rule needs secret context in the window"
    out = scrub.scrub_text("private_strategy note\nrolled >= 0.10\n", rules, is_python=False)
    assert "rolled >= 0.XX" in out.text
    assert out.changes and out.changes[0][1] != out.text


def test_python_code_is_never_rewritten(tmp_path: Path) -> None:
    rules = _rules(tmp_path, "G | \\b0\\.6737\\b | 0.XXXX\n")
    src = (
        '"""Doc 0.XXX."""\nY = 0.XXX  # from 0.XXX\n'
        'def f():\n    """inner 0.XXX"""\n    return 1\n'
    )
    out = scrub.scrub_text(src, rules, is_python=True)
    assert "Y = 0.XXX" in out.text, "a code value must survive"
    assert "# from 0.XXXX" in out.text and "Doc 0.XXXX" in out.text and "inner 0.XXXX" in out.text
    assert out.stops == [2]


def test_residue_finds_leftovers(tmp_path: Path) -> None:
    rules = _rules(tmp_path, "G | \\b0\\.988\\b | 0.XXX\n")
    assert scrub.residue("a\nb 0.XXX\n", rules) == [2]
    with pytest.raises(ValueError):
        _rules(tmp_path, "X | nonsense\n")


def test_is_text_rejects_binary() -> None:
    assert scrub.is_text(b"plain")
    assert not scrub.is_text(b"\x00\x01")
    assert not scrub.is_text(b"\xff\xfe\xfa")


# ---------------------------------------------------------------- end to end
def _write_inputs(tmp_path: Path) -> dict[str, Path]:
    stubs = tmp_path / "stubs"
    (stubs / "config" / "strategies").mkdir(parents=True)
    (stubs / "config" / "strategies" / "private_strategy.py").write_text("STUB_KNOB_VALUE = 1\n")
    deny = tmp_path / "deny.txt"
    deny.write_text(DENY)
    tok = tmp_path / "tokens.txt"
    tok.write_text(TOKENS)
    return {"stubs": stubs, "deny": deny, "tok": tok, "allow": tmp_path / "allow.txt"}


def test_build_end_to_end(repo: Path, tmp_path: Path) -> None:
    p = _write_inputs(tmp_path)
    allow = p["allow"]
    allow.write_text("config/keep.py\n")  # the one deliberate code value (Y = 0.XXX)
    out = tmp_path / "v1"
    result = build.build(repo, "HEAD", out, p["deny"], p["tok"], p["stubs"], allow)
    tree = out / "tree"
    assert not (tree / "docs" / "research").exists() and not (tree / "data").exists()
    assert not (tree / "engine" / "source_secret.py").exists()
    assert (tree / "config/strategies/private_strategy.py").read_text() == "STUB_KNOB_VALUE = 1\n"
    assert "0.XXX" not in (tree / "README.md").read_text()
    assert "Y = 0.XXX" in (tree / "config/keep.py").read_text()
    assert not (tree / "tests/test_uses_recipe.py").exists(), "dependent test must follow out"
    assert (tree / "tests/test_keeps.py").exists()
    m = result.manifest
    assert m["dependent_tests_dropped"] == 1 and m["redactions"] == 2
    assert (out / "MANIFEST.json").exists() and (out / "redaction_report.json").exists()
    report = (out / "redaction_report.json").read_text()
    assert "0.XXX" not in report and "before_sha256" in report, "originals are stored as hashes"
    assert _git(tree, "log", "--oneline").endswith("fixture base")
    assert _git(tree, "status", "--porcelain") == ""


def test_build_fails_on_unapproved_code_value(repo: Path, tmp_path: Path) -> None:
    p = _write_inputs(tmp_path)
    with pytest.raises(build.BuildError, match="stops=1"):
        build.build(repo, "HEAD", tmp_path / "v1", p["deny"], p["tok"], p["stubs"], p["allow"])


def test_build_is_deterministic(repo: Path, tmp_path: Path) -> None:
    p = _write_inputs(tmp_path)
    p["allow"].write_text("config/keep.py\n")
    a = build.build(repo, "HEAD", tmp_path / "a", p["deny"], p["tok"], p["stubs"], p["allow"])
    b = build.build(repo, "HEAD", tmp_path / "b", p["deny"], p["tok"], p["stubs"], p["allow"])
    assert a.manifest["tree_hash"] == b.manifest["tree_hash"]
    assert a.manifest["fixture_base_commit"] == b.manifest["fixture_base_commit"]


def test_identifier_leaks_reports_names_defined_only_in_excluded_files(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "kept.py").write_text("def kept_function_name():\n    pass\n")
    (tree / "README.md").write_text("mentions LONG_RECIPE_NAME and kept_function_name\n")
    excluded = {"gone.py": "LONG_RECIPE_NAME = 5\ndef kept_function_name():\n    pass\n"}
    assert verify.identifier_leaks(excluded, tree, provided=set()) == {"LONG_RECIPE_NAME": 1}
    assert verify.identifier_leaks(excluded, tree, provided={"LONG_RECIPE_NAME"}) == {}
