"""The cyclomatic-complexity ceiling for the harness: ruff's C901 does not count boolean operators.

Depends on: ast (the count follows radon's rules for the constructs used here).
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CEILING = 15
BRANCHES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.Assert, ast.IfExp)


def _own_nodes(func: ast.AST) -> list[ast.AST]:
    """The nodes of `func` outside any nested function, which is counted on its own."""
    found: list[ast.AST] = []
    stack = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        found.append(node)
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            stack.extend(ast.iter_child_nodes(node))
    return found


def complexity(func: ast.AST) -> int:
    """Radon-style complexity of one function: branches, boolean operands, comprehension clauses."""
    total = 1
    for node in _own_nodes(func):
        if isinstance(node, BRANCHES):
            total += 1
        elif isinstance(node, ast.BoolOp):
            total += len(node.values) - 1
        elif isinstance(node, ast.comprehension):
            total += 1 + len(node.ifs)
        elif isinstance(node, ast.match_case):
            total += 1
    return total


def _functions(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text())
    return [
        (f"{path.relative_to(ROOT)}:{n.lineno} {n.name}", complexity(n))
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
    ]


def test_no_harness_function_reaches_the_complexity_ceiling() -> None:
    over = [
        f"{where} = {n}"
        for path in sorted((ROOT / "bench").rglob("*.py"))
        for where, n in _functions(path)
        if n >= CEILING
    ]
    assert not over, over


def test_the_counter_counts_boolean_operands_and_comprehension_filters() -> None:
    func = ast.parse("def f(a, b, c):\n    if a and b or c:\n        return [x for x in a if x]\n")
    assert complexity(func.body[0]) == 1 + 1 + 2 + 2
