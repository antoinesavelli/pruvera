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


OFFBYONE_AFTER = frozenset({"[", ":", "(", ",", ">", "<", ">=", "<=", "==", "+", "-"})


def _mutant_for(toks: list[tokenize.TokenInfo], i: int) -> Mutant | None:
    """The mutant for token `i`, if it is a mutable one."""
    tok = toks[i]
    text, (line, col) = tok.string, tok.start
    if text in FLIPS and tok.type in (tokenize.OP, tokenize.NAME):
        return Mutant(line, col, text, FLIPS[text], "flip")
    if tok.type == tokenize.NAME and text in WORDS:
        return Mutant(line, col, text, WORDS[text], "bool")
    small_number = tok.type == tokenize.NUMBER and text.isdigit() and 1 <= int(text) <= 20
    if small_number and i and toks[i - 1].string in OFFBYONE_AFTER:
        return Mutant(line, col, text, str(int(text) + 1), "offbyone")
    return None


def candidates(source: str) -> list[Mutant]:
    """Every mutable token in `source`, in file order; strings and comments are never touched."""
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError):
        return []
    found = (_mutant_for(toks, i) for i in range(len(toks)))
    return [m for m in found if m is not None]


def apply(source: str, m: Mutant) -> str:
    """`source` with mutant `m` applied; raises if the token is not where the mutant says."""
    lines = source.split("\n")
    row = lines[m.line - 1]
    if row[m.col : m.col + len(m.old)] != m.old:
        raise ValueError(f"line {m.line} col {m.col} does not hold {m.old!r}")
    lines[m.line - 1] = row[: m.col] + m.new + row[m.col + len(m.old) :]
    return "\n".join(lines)
