"""Delete named tests (or whole test classes) from a fixture tree by AST, keeping files valid.

Depends on: fixtures/paramo/drop_tests.txt (owner-approved node ids); stdlib only.
"""

from __future__ import annotations

import ast
from pathlib import Path


class DropError(RuntimeError):
    """A drop entry matched nothing, or a file no longer parses after the edit."""


def load_ids(path: Path) -> list[str]:
    """Node ids from a drop file: `path::func`, `path::Class` or `path::Class::method`."""
    ids: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.split("  #", 1)[0].strip()
        if line and not line.startswith("#"):
            ids.append(line)
    return ids


def _span(node: ast.stmt) -> tuple[int, int]:
    decorators = getattr(node, "decorator_list", [])
    start = min([node.lineno, *[d.lineno for d in decorators]])
    return start, node.end_lineno or node.lineno


def _find(body: list[ast.stmt], names: list[str]) -> ast.stmt | None:
    for node in body:
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            if node.name == names[0]:
                return node if len(names) == 1 else _find_in(node, names[1:])
    return None


def _find_in(node: ast.stmt, names: list[str]) -> ast.stmt | None:
    return _find(node.body, names) if isinstance(node, ast.ClassDef) else None


def drop(tree: Path, ids: list[str]) -> list[str]:
    """Remove every listed test; return the ids applied. An unmatched id raises DropError."""
    by_file: dict[str, list[list[str]]] = {}
    for node_id in ids:
        rel, *names = node_id.split("::")
        by_file.setdefault(rel, []).append(names)
    applied: list[str] = []
    for rel, targets in by_file.items():
        path = tree / rel
        if not path.is_file():
            raise DropError(f"file missing: {rel}")
        source = path.read_text()
        module = ast.parse(source)
        spans: list[tuple[int, int]] = []
        for names in targets:
            node = _find(module.body, names)
            if node is None:
                raise DropError(f"not found: {rel}::{'::'.join(names)}")
            spans.append(_span(node))
            applied.append(f"{rel}::{'::'.join(names)}")
        lines = source.split("\n")
        for start, end in sorted(set(spans), reverse=True):
            del lines[start - 1 : end]
        text = "\n".join(lines)
        try:
            ast.parse(text)
        except SyntaxError as exc:
            raise DropError(f"{rel} does not parse after the drop: {exc}") from exc
        path.write_text(text)
    return applied
