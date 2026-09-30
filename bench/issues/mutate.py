"""Generate single-token mutants of a Python file: the raw material for planted logic bugs.

Each mutant is one exact replacement of one operator, boolean or small integer literal. Whether
a mutant is a real bug (a test kills it) or a survivor (review-only) is decided by running tests,
not here.
Depends on: stdlib only.
"""

from __future__ import annotations

import io
import tokenize
from dataclasses import dataclass

FLIPS = {
    "<": "<=",
    "<=": "<",
    ">": ">=",
    ">=": ">",
    "==": "!=",
    "!=": "==",
    "and": "or",
    "or": "and",
}
WORDS = {"True": "False", "False": "True"}


@dataclass(frozen=True)
class Mutant:
    line: int
    col: int
    old: str
    new: str
    operator: str  # flip | bool | offbyone


def candidates(source: str) -> list[Mutant]:
    """Every mutable token in `source`, in file order; strings and comments are never touched."""
    out: list[Mutant] = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError):
        return out
    for i, tok in enumerate(toks):
        text, (line, col) = tok.string, tok.start
        if tok.type == tokenize.OP and text in FLIPS:
            out.append(Mutant(line, col, text, FLIPS[text], "flip"))
        elif tok.type == tokenize.NAME and text in FLIPS:
            out.append(Mutant(line, col, text, FLIPS[text], "flip"))
        elif tok.type == tokenize.NAME and text in WORDS:
            out.append(Mutant(line, col, text, WORDS[text], "bool"))
        elif tok.type == tokenize.NUMBER and text.isdigit() and 1 <= int(text) <= 20:
            prev = toks[i - 1].string if i else ""
            if prev in {"[", ":", "(", ",", ">", "<", ">=", "<=", "==", "+", "-"}:
                out.append(Mutant(line, col, text, str(int(text) + 1), "offbyone"))
    return out


def apply(source: str, m: Mutant) -> str:
    """`source` with mutant `m` applied; raises if the token is not where the mutant says."""
    lines = source.split("\n")
    row = lines[m.line - 1]
    if row[m.col : m.col + len(m.old)] != m.old:
        raise ValueError(f"line {m.line} col {m.col} does not hold {m.old!r}")
    lines[m.line - 1] = row[: m.col] + m.new + row[m.col + len(m.old) :]
    return "\n".join(lines)
