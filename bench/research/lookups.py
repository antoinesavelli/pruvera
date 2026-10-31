"""Templates L (definition file), V (constant value) and C (counts) of the research screen.

Each builder returns the questions whose structural key (`ast`, a directory listing) matched an
independent raw-text line scan, and the number of candidates dropped because the two disagreed.
Depends on: bench.research (Question), bench.research.treescan; the standard library.
"""

from __future__ import annotations

import ast
import os
import re
from collections import Counter, defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

from bench.research import Kind, Question
from bench.research.treescan import Built, Corpus, Scalar, collect, is_test, make_question, same

_FUNC_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
_DEF_ANY = re.compile(r"^[ \t]*(?:async[ \t]+def|def|class)[ \t]+(\w+)", re.M)
_ASSIGN_LINE = re.compile(r"^(\w+)[ \t]*(?::[^=\n]+)?=(?!=)", re.M)
_CONST_NAME = re.compile(r"[A-Z][A-Z0-9_]{3,}")
_CONST_LINE = re.compile(
    r"^([A-Z][A-Z0-9_]{3,})[ \t]*(?::[^=\n]+)?=[ \t]*"
    r"(-?[\d_]+(?:\.\d+)?(?:[eE][-+]?\d+)?|\"[^\"\\\n]*\"|'[^'\\\n]*')[ \t]*(?:#.*)?$",
    re.M,
)
_TEST_DEF = re.compile(r"^(?:async[ \t]+def|def)[ \t]+test_", re.M)
_TEST_DEF_ANY = re.compile(r"^[ \t]*(?:async[ \t]+def|def)[ \t]+test_", re.M)
_TOP_DEF = re.compile(r"^(?:async[ \t]+def|def)[ \t]", re.M)

# ---- L: where is it defined -------------------------------------------------------------------

_DEF_NODES = (*_FUNC_NODES, ast.ClassDef)


class _Defs(NamedTuple):
    top: dict[str, list[str]]  # name -> files with a top-level def/class, outside tests
    anywhere: dict[str, list[str]]  # name -> files, every def/class at any depth, tests included
    assigned: set[str]  # names bound by a top-level assignment anywhere
    raw: dict[str, list[str]]  # name -> files, by a line scan allowing indentation
    raw_assigned: set[str]  # names at the start of an assignment line


def _ast_defs(c: Corpus) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    top: dict[str, list[str]] = defaultdict(list)
    anywhere: dict[str, list[str]] = defaultdict(list)
    for rel in c.with_suffix(".py"):
        mod = c.module(rel)
        top_ids = {id(n) for n in mod.body} if mod else set()
        for node in ast.walk(mod) if mod else ():
            if isinstance(node, _DEF_NODES):
                anywhere[node.name].append(rel)
                if id(node) in top_ids and not is_test(rel):
                    top[node.name].append(rel)
    return top, anywhere


def _scan_defs(c: Corpus) -> _Defs:
    top, anywhere = _ast_defs(c)
    assigned: set[str] = set()
    raw: dict[str, list[str]] = defaultdict(list)
    raw_assigned: set[str] = set()
    for rel in c.with_suffix(".py"):
        mod = c.module(rel)
        assigned.update(name for name, _ in (_top_assigns(mod) if mod else ()))
        for name in _DEF_ANY.findall(c.text(rel)):
            raw[name].append(rel)
        raw_assigned.update(_ASSIGN_LINE.findall(c.text(rel)))
    return _Defs(top, anywhere, assigned, raw, raw_assigned)


def _l_unique(name: str, files: list[str], defs: _Defs) -> bool:
    """Defined once in the whole tree (any depth, tests too), never assigned, and long enough."""
    return (
        len(files) == 1
        and len(name) >= 14
        and defs.anywhere[name] == files
        and (name not in defs.assigned)
    )


def _l_candidates(c: Corpus, defs: _Defs) -> Iterator[tuple[str, str, list[str]]]:
    """(name, file, other files that mention it): the file name must not give the name away."""
    for name, files in sorted(defs.top.items()):
        if not _l_unique(name, files, defs):
            continue
        callers = [f for f in c.files_with_word(name) if f != files[0]]
        if len(callers) >= 3 and name.lower() not in files[0].rsplit("/", 1)[-1].lower():
            yield name, files[0], callers


def _l_question(defs: _Defs, name: str, rel: str, callers: list[str]) -> Question | None:
    if defs.raw.get(name) != [rel] or name in defs.raw_assigned:
        return None
    text = f"In which file is the function or class `{name}` defined? Give the repo-relative path."
    return make_question("L", name, text, "path", rel, [rel], callers)


def build_l(c: Corpus) -> Built:
    """Long names defined once in the tree, outside tests, in a file whose name does not say so."""
    defs = _scan_defs(c)
    return collect(_l_question(defs, *cand) for cand in _l_candidates(c, defs))


# ---- V: module-level constant ------------------------------------------------------------------


def _const_value(node: ast.expr | None) -> Scalar | None:
    """A number or short plain string the node evaluates to, else None."""
    if node is None:
        return None
    try:
        value = ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    if isinstance(value, str) and not (
        0 < len(value) <= 40 and value.isprintable() and not set(value) & set("\"'\\")
    ):
        return None
    return value


def _top_assigns(mod: ast.Module) -> list[tuple[str, Scalar | None]]:
    """(name, literal value or None) for each module-level binding, in source order."""
    out: list[tuple[str, Scalar | None]] = []
    for node in mod.body:
        if isinstance(node, ast.Assign):
            value = _const_value(node.value) if len(node.targets) == 1 else None
            out.extend((t.id, value) for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.append((node.target.id, _const_value(node.value)))
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            out.append((node.target.id, None))
    return out


def _raw_value(token: str) -> Scalar | None:
    if token[0] in "\"'":
        return token[1:-1]
    try:
        return int(token)
    except ValueError:
        pass
    try:
        return float(token)
    except ValueError:
        return None


def _doc_values(c: Corpus, name: str, value: Scalar) -> list[str]:
    """Other numbers that a doc states right after the constant's name."""
    if isinstance(value, str):
        return []
    pattern = re.compile(rf"\b{re.escape(name)}\b[^\n]{{0,60}}?(-?\d+(?:\.\d+)?)")
    found: list[str] = []
    for rel in c.with_suffix(".md"):
        if name in c.text(rel):
            found.extend(m for m in pattern.findall(c.text(rel)) if float(m) != value)
    return found


class _Consts(NamedTuple):
    seen: dict[str, list[tuple[str, int]]]  # name -> (file, position) of each binding, by ast
    raw: dict[str, list[tuple[str, Scalar | None]]]  # name -> (file, value), by line scan
    per_file: dict[str, list[tuple[str, Scalar | None]]]
    stores: Counter[str]  # name -> every store or `global`/`nonlocal` of it, at any depth


def _store_names(mod: ast.Module) -> Iterator[str]:
    """Each name stored anywhere in the module (assignments, loops, walrus) or declared global."""
    for node in ast.walk(mod):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            yield node.id
        elif isinstance(node, ast.Global | ast.Nonlocal):
            yield from node.names


def _scan_consts(c: Corpus) -> _Consts:
    seen: dict[str, list[tuple[str, int]]] = defaultdict(list)
    raw: dict[str, list[tuple[str, Scalar | None]]] = defaultdict(list)
    per_file: dict[str, list[tuple[str, Scalar | None]]] = {}
    stores: Counter[str] = Counter()
    for rel in c.with_suffix(".py"):
        mod = c.module(rel)
        per_file[rel] = _top_assigns(mod) if mod else []
        stores.update(_store_names(mod) if mod else ())
        for i, (name, _) in enumerate(per_file[rel]):
            seen[name].append((rel, i))
        for name, token in _CONST_LINE.findall(c.text(rel)):
            raw[name].append((rel, _raw_value(token)))
    return _Consts(seen, raw, per_file, stores)


def _v_candidates(scan: _Consts) -> Iterator[tuple[str, str, int, Scalar]]:
    """(name, file, position, value) of each constant bound once, never rebound, outside tests."""
    for name, places in sorted(scan.seen.items()):
        if len(places) != 1 or scan.stores[name] != 1 or not _CONST_NAME.fullmatch(name):
            continue
        rel, i = places[0]
        value = scan.per_file[rel][i][1]
        if value is not None and not is_test(rel):
            yield name, rel, i, value


def _neighbours(items: list[tuple[str, Scalar | None]], i: int, value: Scalar) -> list[str]:
    """The literal values bound just before and after position `i` when they differ."""
    near = [items[j][1] for j in (i - 1, i + 1) if 0 <= j < len(items)]
    return [str(v) for v in near if v is not None and v != value]


def _v_question(c: Corpus, scan: _Consts, cand: tuple[str, str, int, Scalar]) -> Question | None:
    name, rel, i, value = cand
    found = scan.raw.get(name, [])
    if len(found) != 1 or found[0][0] != rel or not same(found[0][1], value):
        return None
    decoys = _neighbours(scan.per_file[rel], i, value) + _doc_values(c, name, value)
    text = f"What value does the module-level constant `{name}` have in the current code?"
    kind: Kind = "string" if isinstance(value, str) else "number"
    return make_question("V", name, text, kind, value, [rel], decoys)


def build_v(c: Corpus) -> Built:
    """Constants assigned exactly once in the whole tree, to a number or a short string."""
    scan = _scan_consts(c)
    return collect(_v_question(c, scan, cand) for cand in _v_candidates(scan))


# ---- C: counts ---------------------------------------------------------------------------------


def _module_level_tests(mod: ast.Module) -> int:
    return sum(1 for x in mod.body if isinstance(x, _FUNC_NODES) and x.name.startswith("test_"))


def _test_files(c: Corpus) -> Iterator[tuple[str, int]]:
    """(file, module-level test count) for test modules with 8 to 25 tests."""
    for rel in c.with_suffix(".py"):
        mod = c.module(rel)
        if mod is None or not rel.rsplit("/", 1)[-1].startswith("test_"):
            continue
        n = _module_level_tests(mod)
        if 8 <= n <= 25:
            yield rel, n


def _tests_question(c: Corpus, rel: str, n: int) -> Question | None:
    text = c.text(rel)
    if len(_TEST_DEF.findall(text)) != n:
        return None
    wrong = [len(_TEST_DEF_ANY.findall(text)), len(_TOP_DEF.findall(text))]
    question = f"How many functions named `test_*` are defined at module level in `{rel}`?"
    return make_question("C", f"tests-{rel}", question, "number", n, [rel], _others(wrong, n))


def _others(counts: list[int], n: int) -> list[str]:
    return [str(w) for w in counts if w != n]


def _list_py(path: Path) -> list[str]:
    """Names of the visible `.py` files directly inside `path`, `__init__.py` excluded."""
    with os.scandir(path) as entries:
        return sorted(
            e.name
            for e in entries
            if e.is_file(follow_symlinks=False)
            and e.name.endswith(".py")
            and not e.name.startswith(".")
            and e.name != "__init__.py"
        )


def _dirs(c: Corpus) -> Iterator[tuple[str, list[str]]]:
    """(directory, its direct `.py` file names) for directories holding 6 to 20 of them."""
    for d in sorted({f.rsplit("/", 1)[0] for f in c.with_suffix(".py") if "/" in f}):
        listing = _list_py(c.root / d)
        if 6 <= len(listing) <= 20:
            yield d, listing


def _dir_question(c: Corpus, d: str, listing: list[str]) -> Question | None:
    n = len(listing)
    direct = re.compile(rf"{re.escape(d)}/(?!__init__\.py$)[^/]+\.py")
    if sum(1 for f in c.files if direct.fullmatch(f)) != n:
        return None
    below = [f for f in c.with_suffix(".py") if f.startswith(f"{d}/")]
    wrong = [n + (f"{d}/__init__.py" in c.files), len(below) - (f"{d}/__init__.py" in below)]
    question = (
        f"How many `.py` files, not counting `__init__.py`, are directly inside `{d}/` "
        "(not its subfolders)?"
    )
    sources = [f"{d}/{name}" for name in listing]
    return make_question("C", f"files-{d}", question, "number", n, sources, _others(wrong, n))


def build_c(c: Corpus) -> Built:
    """Module-level test counts per test file, and direct `.py` file counts per directory."""
    tests, bad_tests = collect(_tests_question(c, rel, n) for rel, n in _test_files(c))
    dirs, bad_dirs = collect(_dir_question(c, d, listing) for d, listing in _dirs(c))
    return tests + dirs, bad_tests + bad_dirs
