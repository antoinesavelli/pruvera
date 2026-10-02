"""Docstring claims that can be checked are checked: `Depends on` lines match the real imports."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULES = sorted(p for p in (ROOT / "bench").rglob("*.py") if p.name != "__init__.py")
DEPENDS = re.compile(r"Depends on:?(.*?)(?:\n\n|\Z|\"\"\")", re.S)
BRACES = re.compile(r"(bench(?:\.\w+)*)\.\{([^}]*)\}")
NAME = re.compile(r"bench(?:\.\w+)+")


def _imports(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("bench"):
            if (ROOT / node.module.replace(".", "/")).is_dir():  # a package: its names are modules
                found |= {f"{node.module}.{a.name}" for a in node.names}
            else:
                found.add(node.module)
        elif isinstance(node, ast.Import):
            found |= {a.name for a in node.names if a.name.startswith("bench.")}
    return found


def _named(doc: str) -> set[str]:
    match = DEPENDS.search(doc)
    text = match.group(1) if match else ""
    names = set(NAME.findall(BRACES.sub("", text)))
    for prefix, members in BRACES.findall(text):
        names |= {f"{prefix}.{m.strip()}" for m in members.split(",") if m.strip()}
    return names


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_the_depends_on_line_names_exactly_the_bench_modules_a_module_imports(path: Path) -> None:
    tree = ast.parse(path.read_text())
    doc = ast.get_docstring(tree) or ""
    assert "Depends on" in doc, f"{path.name} has no `Depends on` line"
    imported = _imports(tree)
    named = _named(doc)
    own = "bench." + ".".join(path.relative_to(ROOT / "bench").with_suffix("").parts)
    missing = sorted(
        m for m in imported if m != own and not any(n == m or m.startswith(n + ".") for n in named)
    )
    extra = sorted(
        n for n in named if n != own and not any(m == n or m.startswith(n + ".") for m in imported)
    )
    assert not missing and not extra, (
        f"imports not named: {missing}; named but not imported: {extra}"
    )


def _logical_lines(path: Path) -> int:
    import io
    import tokenize

    tokens = tokenize.generate_tokens(io.StringIO(path.read_text()).readline)
    return sum(1 for t in tokens if t.type == tokenize.NEWLINE)


def test_no_harness_module_is_over_the_500_logical_line_ceiling() -> None:
    """PLAN section 9 promises modules under 500 functional lines; counted here, not claimed."""
    sizes = {p.relative_to(ROOT).as_posix(): _logical_lines(p) for p in MODULES}
    assert {k: v for k, v in sizes.items() if v >= 500} == {}, "split the module"
    assert max(sizes.values()) > 100, "the counter counts something"


def _stems(paths: list[Path]) -> set[str]:
    return {p.stem for p in paths}


def test_every_harness_module_is_named_in_the_module_tables() -> None:
    """The READMEs list modules by name; a new module that is not listed is a doc defect."""
    tables = (ROOT / "bench" / "README.md").read_text()
    top = (ROOT / "README.md").read_text()

    def listed(stem: str, text: str) -> bool:
        return re.search(rf"`{stem}(\.py)?`", text) is not None

    missing_in_bench = sorted(s for s in _stems(MODULES) if not listed(s, tables))
    top_level = _stems([p for p in MODULES if p.parent == ROOT / "bench"])
    missing_in_top = sorted(s for s in top_level if not listed(s, top))
    assert missing_in_bench == [] and missing_in_top == [], (missing_in_bench, missing_in_top)
