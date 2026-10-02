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
    assert restore._same_ast("def (", ORIGINAL) is None, "a file that does not parse"


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
    assert ("a b" in [t[1] for t in tokens] or '"a b"' in [t[1] for t in tokens]) and not any(
        "# c" in t[1] for t in tokens
    )
    assert restore._contains(tokens, restore._code_tokens("y = 1")) is True
    assert restore._contains(tokens, []) is False
