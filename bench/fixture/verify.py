"""Acceptance checks on a built fixture tree: no ciphertext, no excluded paths, no token residue.

Depends on: bench.fixture.denylist, bench.fixture.scrub, bench.fixture.export (ciphertext header).
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

from bench.fixture import denylist, export, scrub


@dataclass
class Report:
    """The acceptance findings for a built tree; it passes when every field is empty."""

    ciphertext: list[str] = field(default_factory=list)
    denylisted: list[str] = field(default_factory=list)
    residue: dict[str, list[int]] = field(default_factory=dict)
    stops: dict[str, list[int]] = field(
        default_factory=dict
    )  # code-region matches awaiting a decision

    @property
    def ok(self) -> bool:
        """True when no check found a problem: no ciphertext, denylisted path, residue or stop."""
        return not (self.ciphertext or self.denylisted or self.residue or self.stops)


def _tree_paths(tree: Path) -> list[str]:
    return sorted(
        p.relative_to(tree).as_posix()
        for p in tree.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(tree).parts[:1]
    )


def check_tree(
    tree: Path,
    deny_rules: list[str],
    token_rules: list[scrub.Rule],
    allowed_code_matches: frozenset[str] = frozenset(),
    stub_paths: frozenset[str] = frozenset(),
) -> Report:
    """Every acceptance check that can be run without the source repo."""
    report = Report(ciphertext=export.ciphertext_files(tree))
    paths = _tree_paths(tree)
    report.denylisted = sorted(denylist.excluded(paths, deny_rules) - stub_paths)  # stubs replace
    for rel in paths:
        data = (tree / rel).read_bytes()
        if not scrub.is_text(data):
            continue
        text = data.decode("utf-8")
        if rel.endswith(".py"):
            res = scrub.scrub_text(text, token_rules, True, rel)
            code_hits = res.stops
            prose_hits = [ln for ln, _, _ in res.changes]  # prose still matching means unredacted
            if code_hits and rel not in allowed_code_matches:
                report.stops[rel] = code_hits
            if prose_hits:
                report.residue[rel] = prose_hits
        else:
            hits = scrub.residue(text, token_rules)
            if hits:
                report.residue[rel] = hits
    return report


_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{9,}")


def _node_names(node: ast.AST) -> set[str]:
    """Names a single AST node defines that matter for leak detection."""
    if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
        return {node.name}
    if isinstance(node, ast.Assign):
        return {t.id for t in node.targets if isinstance(t, ast.Name) and t.id.isupper()}
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return {node.target.id}
    return set()


def defined_names(source: str) -> set[str]:
    """Long, distinctive names a Python file defines (classes, functions, constants)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names = set().union(*(_node_names(n) for n in ast.walk(tree)))
    return {n for n in names if len(n) >= 10 and ("_" in n or n != n.lower())}


def identifier_leaks(
    excluded_sources: dict[str, str], tree: Path, provided: set[str]
) -> dict[str, int]:
    """Names defined only in excluded files that still appear in kept text (files per name)."""
    # Expected: names the stubs provide, and prose mentions. Anything else is a dangling reference
    # or a leaked concept and is reviewed by the owner, not auto-failed.
    kept_text: dict[str, str] = {}
    for rel in _tree_paths(tree):
        data = (tree / rel).read_bytes()
        if scrub.is_text(data):
            kept_text[rel] = data.decode("utf-8")
    kept_defined: set[str] = set()
    for rel, text in kept_text.items():
        if rel.endswith(".py"):
            kept_defined |= defined_names(text)
    only_excluded: set[str] = set()
    for source in excluded_sources.values():
        only_excluded |= defined_names(source)
    only_excluded -= kept_defined | provided
    seen: dict[str, int] = {}
    for text in kept_text.values():
        for name in set(_IDENT.findall(text)):
            seen[name] = seen.get(name, 0) + 1
    return {name: seen[name] for name in sorted(only_excluded) if name in seen}
