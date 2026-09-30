"""Redact recipe-derived numbers from kept files, and report what would need an owner decision.

Rules come from a gitignored local token file (never committed). In `.py` files only comments and
docstrings are redacted; a match in real code is reported as a stop, never rewritten (ask-first
values live there). Depends on: stdlib only.
"""

from __future__ import annotations

import ast
import hashlib
import io
import re
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

SECRET_WINDOW = 2
SECRET_CONTEXT = re.compile(
    r"private_strategy|\bprivate_strategy\b|frozen cell|deployed cell|MIN_B|MIN_C|MIN_A", re.I
)


@dataclass(frozen=True)
class Rule:
    kind: str  # "G" global, "C" contextual
    context: re.Pattern[str] | None
    pattern: re.Pattern[str]
    placeholder: str


@dataclass
class Result:
    text: str
    changes: list[tuple[int, str, str]] = field(
        default_factory=list
    )  # (line, sha256 before, after)
    stops: list[int] = field(default_factory=list)  # line numbers with a match in real code


def load_rules(path: Path) -> list[Rule]:
    rules: list[Rule] = []
    for raw in path.read_text().splitlines():
        line = re.split(r"\s{2,}#", raw, maxsplit=1)[0].rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = [p.strip() for p in line.split(" | ")]
        if parts[0] == "G" and len(parts) == 3:
            rules.append(Rule("G", None, re.compile(parts[1], re.I), parts[2]))
        elif parts[0] == "C" and len(parts) == 4:
            rules.append(
                Rule("C", re.compile(parts[1], re.I), re.compile(parts[2], re.I), parts[3])
            )
        else:
            raise ValueError(f"unparseable token rule: {raw[:60]!r}")
    return rules


def is_text(data: bytes) -> bool:
    if b"\0" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _applies(rule: Rule, lines: list[str], i: int) -> bool:
    if rule.context is None:
        return True
    if not rule.context.search(lines[i]):
        return False
    window = " ".join(lines[max(0, i - SECRET_WINDOW) : i + SECRET_WINDOW + 1])
    return bool(SECRET_CONTEXT.search(window))


def _redact_line(rules: list[Rule], lines: list[str], i: int, line: str) -> str:
    for rule in rules:
        if _applies(rule, lines, i):
            line = rule.pattern.sub(rule.placeholder, line)
    return line


def _matches(rules: list[Rule], lines: list[str], i: int, line: str) -> bool:
    return any(_applies(rule, lines, i) and rule.pattern.search(line) for rule in rules)


def _prose_lines(source: str) -> set[int] | None:
    """1-based line numbers that are comments or docstring text; None if the file will not parse."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    prose: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            first = body[0] if body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                prose.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT and not tok.line[: tok.start[1]].strip():
                prose.add(
                    tok.start[0]
                )  # a comment-only line; a trailing comment sits on a code line
    except (tokenize.TokenError, IndentationError):
        return None
    return prose


def _code_comment_split(line: str) -> tuple[str, str]:
    """Split a code line at a trailing `#` comment (naive but quote-aware)."""
    quote = ""
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "#":
            return line[:i], line[i:]
    return line, ""


def scrub_text(text: str, rules: list[Rule], is_python: bool) -> Result:
    """Redact `text`; Python files are redacted in prose only, code matches become stops."""
    lines = text.split("\n")
    prose = _prose_lines(text) if is_python else None
    result = Result(text)
    for i, line in enumerate(lines):
        if is_python and prose is not None and (i + 1) not in prose:
            code, comment = _code_comment_split(line)
            if _matches(rules, lines, i, code):
                result.stops.append(i + 1)
            new = code + _redact_line(rules, lines, i, comment) if comment else line
        elif is_python and prose is None:
            new = line  # unparseable Python: leave alone, verification will flag any residue
        else:
            new = _redact_line(rules, lines, i, line)
        if new != line:
            result.changes.append((i + 1, hashlib.sha256(line.encode()).hexdigest(), new))
            lines[i] = new
    result.text = "\n".join(lines)
    return result


def residue(text: str, rules: list[Rule]) -> list[int]:
    """1-based lines where any rule still matches (used after redaction to prove completeness)."""
    lines = text.split("\n")
    return [i + 1 for i, line in enumerate(lines) if _matches(rules, lines, i, line)]
