"""Exclusion rules for the paramo fixture: parse denylist.txt, match paths, derive dependent tests.

Depends on: fixtures/paramo/denylist.txt (owner-reviewed); stdlib only.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from pathlib import Path


def load_rules(path: Path) -> list[str]:
    """Glob patterns from a denylist file; blank lines and `#` comments (also trailing) dropped."""
    rules: list[str] = []
    for raw in path.read_text().splitlines():
        line = re.split(r"\s+#", raw, maxsplit=1)[0] if not raw.lstrip().startswith("#") else ""
        if line.strip():
            rules.append(line.strip())
    return rules


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """`**` crosses directories (a leading `**/` also matches none), `*` and `?` stay inside one."""
    out = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out + r"\Z")


def excluded(paths: Iterable[str], rules: list[str]) -> set[str]:
    """Paths (repo-relative, posix) matched by any rule."""
    compiled = [glob_to_regex(r) for r in rules]
    return {p for p in paths if any(rx.match(p) for rx in compiled)}


def module_name(path: str) -> str | None:
    """Dotted module for a .py path (`a/b/__init__.py` -> `a.b`), or None for non-Python files."""
    if not path.endswith(".py"):
        return None
    parts = path[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else None


def _imported_modules(source: str) -> set[str]:
    """Every dotted name a file imports, including `from pkg import mod` as `pkg.mod`."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def imports_excluded(source: str, gone: set[str]) -> bool:
    """True if the file imports a module that is gone (exact name or below a removed package)."""
    return any(
        m in gone or any(m.startswith(g + ".") for g in gone) for m in _imported_modules(source)
    )


def dependent_files(tree: Path, paths: Iterable[str], gone: set[str], prefix: str = "") -> set[str]:
    """Python files under `prefix` that import a removed module, iterated to a fixpoint.

    A file that imports something removed would crash on import, so it follows the module out; its
    own dependents then follow it. `gone` is not modified.
    """
    gone = set(gone)
    remaining = {p for p in paths if p.startswith(prefix) and p.endswith(".py")}
    dropped: set[str] = set()
    changed = True
    while changed:
        changed = False
        for rel in sorted(remaining - dropped):
            path = tree / rel
            if path.is_file() and imports_excluded(path.read_text(errors="replace"), gone):
                dropped.add(rel)
                if mod := module_name(rel):
                    gone.add(mod)
                changed = True
    return dropped


def dependent_tests(
    tree: Path, paths: Iterable[str], gone: set[str], prefix: str = "tests/"
) -> set[str]:
    """Test files under `prefix` that import a removed module (one pass, no cascade)."""
    return {
        rel
        for rel in paths
        if rel.startswith(prefix)
        and rel.endswith(".py")
        and (tree / rel).is_file()
        and imports_excluded((tree / rel).read_text(errors="replace"), gone)
    }
