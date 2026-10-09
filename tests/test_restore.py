"""bench.issues.restore: has the reference fix been put back in the final files?"""

from __future__ import annotations

from pathlib import Path

from bench.issues import restore
from bench.issues.schema import Edit, Issue
from tests.helpers import make_issue

ORIGINAL = 'def f(a):\n    """Doc."""\n    return a + 1\n'


def _tree(tmp_path: Path, planted: str) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text(planted)
    return tmp_path


def _issue() -> Issue:
    return make_issue(
        detector="review_only", edits=(Edit("pkg/m.py", "return a + 1", "return a - 1"),)
    )


def test_original_text_is_the_planted_file_with_every_edit_reversed(tmp_path: Path) -> None:
    tree = _tree(tmp_path, ORIGINAL.replace("a + 1", "a - 1"))
    assert restore._original(tree, _issue(), "pkg/m.py") == ORIGINAL
    assert restore._original(tree, _issue(), "pkg/absent.py") is None


def test_ast_equality_ignores_comments_layout_and_docstrings_but_not_code() -> None:
    assert restore._same_ast(ORIGINAL, ORIGINAL.replace("Doc.", "Other.")) is True
    assert restore._same_ast(ORIGINAL, ORIGINAL + "# note\n") is True
    assert restore._same_ast(ORIGINAL, ORIGINAL.replace("a + 1", "a  +  1")) is True
    assert restore._same_ast(ORIGINAL, ORIGINAL.replace("a + 1", "a - 1")) is False
    assert restore._same_ast("def (", ORIGINAL) is False, "a broken final file is not a restoration"
    assert restore._same_ast(ORIGINAL, "def (") is None, "an unparsable original is undecidable"


def test_a_docstring_only_function_body_survives_stripping() -> None:
    assert restore._same_ast('def f():\n    """Only."""\n', "def f():\n    pass\n") is True


def test_the_fragment_fallback_is_used_for_non_python_files_and_missing_trees() -> None:
    issue = make_issue(detector="review_only", edits=(Edit("README.md", "old text", "new text"),))
    assert restore.restored(issue, {"README.md": "old text here\n"})
    assert not restore.restored(issue, {"README.md": "new text here\n"})
    assert not restore.restored(issue, {})
    assert not restore.restored(issue, {"README.md": None})


def test_code_tokens_keep_strings_whole_and_drop_comments() -> None:
    tokens = restore._code_tokens('x = "a b"  # c\ny = 1\n')
    texts = [t[1] for t in tokens]
    assert any(t in ("a b", '"a b"') for t in texts) and not any("# c" in t for t in texts)
    assert restore._contains(tokens, restore._code_tokens("y = 1")) is True
    assert restore._contains(tokens, []) is False


def test_a_fixed_file_with_a_syntax_error_appended_is_not_a_restoration(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "m.py").write_text("def f():\n    return 2\n")
    issue = make_issue(detector="review_only", edits=(Edit("m.py", "return 1", "return 2"),))
    good = "def f():\n    return 1\n"
    assert restore.restored(issue, {"m.py": good}, tree)
    assert not restore.restored(issue, {"m.py": good + "\ndef broken(:\n pass\n"}, tree)


def test_a_change_only_in_a_docstring_is_graded_on_its_text(tmp_path: Path) -> None:
    # The AST comparison ignores docstrings, so a docstring-only issue looked fixed while still
    # planted (a doc-drift issue in a module docstring, 2026-10-09). Its text decides instead, and
    # the code must still match the original.
    original = '"""Mod.\n\nDepends on:\n- pkg.a: helpers.\n"""\n\n\ndef f(a):\n    return a + 1\n'
    old, new = "- pkg.a: helpers.\n", "- pkg.a, pkg.gone: helpers.\n"
    planted = original.replace(old, new)
    tree = _tree(tmp_path, planted)
    issue = make_issue(detector="review_only", edits=(Edit("pkg/m.py", old, new),))
    assert not restore.restored(issue, {"pkg/m.py": planted}, tree), "still planted"
    assert restore.restored(issue, {"pkg/m.py": original}, tree), "the docstring fixed"
    pasted = planted + "# " + old
    assert not restore.restored(issue, {"pkg/m.py": pasted}, tree), "planted text still there"
    broken = original.replace("a + 1", "a - 1")
    assert not restore.restored(issue, {"pkg/m.py": broken}, tree), "the code must match too"


def test_a_code_change_is_still_graded_on_its_ast(tmp_path: Path) -> None:
    tree = _tree(tmp_path, ORIGINAL.replace("a + 1", "a - 1"))
    fixed_in_docstring = ORIGINAL.replace("a + 1", "a - 1").replace("Doc.", "return a + 1")
    assert restore.restored(_issue(), {"pkg/m.py": ORIGINAL}, tree)
    assert not restore.restored(_issue(), {"pkg/m.py": fixed_in_docstring}, tree)
