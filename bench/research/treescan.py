"""Read-only scan of a source tree, shared by the research screen's oracles.

Lists the tree's files without hidden dirs, caches, virtual envs, `node_modules` or the answer-key
area `docs/eval`, and reads each file at most once as text, as an `ast` module and as a word set.
Nothing is imported, executed or written.
Depends on: bench.research (Question); the standard library.
"""

from __future__ import annotations

import ast
import hashlib
import io
import os
import re
import tokenize
import warnings
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

from bench.research import Key, Kind, Question

SKIP_DIRS = frozenset({"__pycache__", ".venv", "venv", "node_modules"})
ANSWER_KEY_DIR = "docs/eval"
Scalar = int | float | str
Built = tuple[list[Question], int]  # accepted questions, candidates dropped on disagreement
_TEXT_TOKENS = (tokenize.COMMENT, tokenize.STRING, tokenize.FSTRING_MIDDLE)
_IDENT = re.compile(r"[A-Za-z_][\w.]*")
_FROM_LINE = re.compile(r"^[ \t]*from[ \t]+([A-Za-z_][\w.]*)[ \t]+import\b", re.M)
_IMPORT_LINE = re.compile(r"^[ \t]*import[ \t]+([^#;\n]+)", re.M)


def walk(root: Path) -> list[str]:
    """Sorted repo-relative POSIX paths of every readable regular file the oracles may use."""
    found: list[str] = []
    for cur, dirs, names in os.walk(root):
        here = Path(cur)
        rel = here.relative_to(root).as_posix()
        dirs[:] = sorted(
            d
            for d in dirs
            if not d.startswith(".")
            and d not in SKIP_DIRS
            and (d if rel == "." else f"{rel}/{d}") != ANSWER_KEY_DIR
        )
        for name in names:
            if not name.startswith(".") and not (here / name).is_symlink():
                found.append((here / name).relative_to(root).as_posix())
    return sorted(found)


def is_test(rel: str) -> bool:
    """True for a file under a `tests` directory or named like a test module."""
    *dirs, name = rel.split("/")
    return (
        "tests" in dirs
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
    )


def qid(template: str, name: str) -> str:
    """A stable question id `<template>-<slug>`; a hash is added when the slug lost characters."""
    slug = re.sub(r"[^\w.-]+", "-", name.replace("/", "__")).strip("-")
    if slug != name.replace("/", "__") or len(slug) > 60:
        slug = f"{slug[:48]}-{hashlib.sha1(name.encode()).hexdigest()[:6]}"
    return f"{template}-{slug}"


def make_question(
    template: str,
    name: str,
    text: str,
    kind: Kind,
    key: Key,
    sources: Iterable[str],
    decoys: Iterable[str],
) -> Question:
    """A question with a stable id and its decoys de-duplicated and capped at 6."""
    capped = tuple(dict.fromkeys(decoys))[:6]
    return Question(qid(template, name), template, text, kind, key, tuple(sources), capped)


def collect(results: Iterable[Question | None]) -> Built:
    """Split builder results: questions kept, and the count of None (the two methods disagreed)."""
    items = list(results)
    return [r for r in items if r is not None], sum(1 for r in items if r is None)


def same(a: Scalar | None, b: Scalar | None) -> bool:
    """Equal values of the same type (so 5 and 5.0 differ)."""
    return a is not None and type(a) is type(b) and a == b


def regex_imports(text: str) -> set[str]:
    """Module names on `import`/`from ... import` lines, found by a raw line scan (no parsing)."""
    mods = set(_FROM_LINE.findall(text))
    for clause in _IMPORT_LINE.findall(text):
        for part in clause.split(","):
            words = part.split()
            if words and _IDENT.fullmatch(words[0]):
                mods.add(words[0])
    return mods


def ast_imports(mod: ast.Module) -> tuple[set[str], set[str]]:
    """(modules named by absolute imports, `pkg.member` names pulled in by from-imports)."""
    modules: set[str] = set()
    members: set[str] = set()
    for node in ast.walk(mod):
        if isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
            members.update(f"{node.module}.{a.name}" for a in node.names)
    return modules, members


class Corpus:
    """The files of one tree, each read lazily and once."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.files = walk(root)
        self._text: dict[str, str] = {}
        self._module: dict[str, ast.Module | None] = {}
        self._words: dict[str, frozenset[str]] = {}
        self._index: dict[str, dict[str, list[str]]] = {}
        self._tokens: dict[str, tuple[frozenset[str], frozenset[str]]] = {}

    def with_suffix(self, suffix: str) -> list[str]:
        return [f for f in self.files if f.endswith(suffix)]

    def text(self, rel: str) -> str:
        if rel not in self._text:
            try:
                self._text[rel] = (self.root / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                self._text[rel] = ""
        return self._text[rel]

    def module(self, rel: str) -> ast.Module | None:
        """The parsed module, or None when the file is not valid Python."""
        if rel not in self._module:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                try:
                    self._module[rel] = ast.parse(self.text(rel))
                except (SyntaxError, ValueError, RecursionError):
                    self._module[rel] = None
        return self._module[rel]

    def words(self, rel: str) -> frozenset[str]:
        if rel not in self._words:
            self._words[rel] = frozenset(re.findall(r"\w+", self.text(rel)))
        return self._words[rel]

    def files_with_word(self, word: str, suffix: str = ".py") -> list[str]:
        """Sorted files with this suffix that contain `word` as a whole word (comments included)."""
        if suffix not in self._index:
            index: dict[str, list[str]] = defaultdict(list)
            for rel in self.with_suffix(suffix):
                for found in self.words(rel):
                    index[found].append(rel)
            self._index[suffix] = index
        return self._index[suffix].get(word, [])

    def token_words(self, rel: str) -> tuple[frozenset[str], frozenset[str]]:
        """(words inside comments and string literals, identifiers) of a Python file."""
        if rel not in self._tokens:
            in_text: set[str] = set()
            names: set[str] = set()
            try:
                for tok in tokenize.generate_tokens(io.StringIO(self.text(rel)).readline):
                    if tok.type in _TEXT_TOKENS:
                        in_text.update(re.findall(r"\w+", tok.string))
                    elif tok.type == tokenize.NAME:
                        names.add(tok.string)
            except (tokenize.TokenError, SyntaxError):
                pass
            self._tokens[rel] = (frozenset(in_text), frozenset(names))
        return self._tokens[rel]
