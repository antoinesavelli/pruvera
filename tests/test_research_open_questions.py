"""Self-test of the open-question loader and anchor check on a synthetic tree and questions."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("yaml")

from bench.research import open_questions as oq  # noqa: E402

SEED = 7
PER_KIND = 8
MODULE = "def alpha():\n    return 1\n\nBETA = 2\n"  # four lines
NOTES = "# notes\nthe gamma helper lives in pkg/mod.py\n"


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A tree at `tree/`, with a real file beside it that a `..` path could reach."""
    root = tmp_path / "tree"
    (root / "pkg").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "pkg" / "mod.py").write_text(MODULE)
    (root / "pkg" / "blob.bin").write_bytes(b"BETA\0\x01")
    (root / "docs").mkdir()
    (root / "docs" / "notes.md").write_text(NOTES)
    (root / ".git" / "config.py").write_text("BETA = 9\n")
    (tmp_path / "outside.txt").write_text("BETA\n")
    return root


def _anchor(path: str = "pkg/mod.py", line: int = 4, text: str = "BETA") -> dict[str, Any]:
    return {"path": path, "line": line, "text": text}


def _search(matches: list[str], pattern: str = "BETA", target: str = "line") -> dict[str, Any]:
    return {
        "pattern": pattern,
        "include": ["pkg/*.py"],
        "exclude": [],
        "target": target,
        "matches": matches,
    }


def _question(qid: str, kind: str) -> dict[str, Any]:
    fact: dict[str, Any] = {"id": "r1", "text": f"{qid} fact", "anchors": [_anchor()]}
    question: dict[str, Any] = {
        "id": qid,
        "kind": kind,
        "text": f"{qid} question",
        "required": [fact],
        "forbidden": [{"id": "f1", "text": "a wrong claim"}],
        "no_citation_ok": kind == "Negative",
    }
    if kind == "Negative":
        fact["anchors"] = []
        fact["search"] = [_search([], pattern="NOWHERE")]
    if kind == "Locate-all":
        question["search"] = [_search(["pkg/mod.py:4"])]
    return question


def _valid() -> dict[str, dict[str, Any]]:
    """Forty questions, eight per kind, split by the seeded shuffle, as one document per split."""
    ids = {kind: [f"{kind[0]}{n}" for n in range(1, PER_KIND + 1)] for kind in oq.KINDS}
    where = oq.assign_splits(ids, SEED)
    docs: dict[str, dict[str, Any]] = {
        split: {"split": split, "seed": SEED, "questions": []} for split in oq.SPLITS
    }
    for kind, kind_ids in ids.items():
        for qid in kind_ids:
            docs[where[qid]]["questions"].append(_question(qid, kind))
    return docs


def _write(directory: Path, docs: dict[str, dict[str, Any]]) -> None:
    directory.mkdir(exist_ok=True)
    for split, doc in docs.items():
        (directory / f"{split}.json").write_text(json.dumps(doc))


def _failures(tmp_path: Path, tree: Path, docs: dict[str, dict[str, Any]], **kw: bool) -> list[str]:
    qdir = tmp_path / "questions"
    _write(qdir, docs)
    return oq.check(qdir, tree, **kw)[1]


def _find(docs: dict[str, dict[str, Any]], qid: str) -> dict[str, Any]:
    return next(q for d in docs.values() for q in d["questions"] if q["id"] == qid)


def _first_of(docs: dict[str, dict[str, Any]], split: str, kind: str) -> dict[str, Any]:
    return next(q for q in docs[split]["questions"] if q["kind"] == kind)


def test_a_valid_set_passes_and_reports_counts(tmp_path: Path, tree: Path) -> None:
    qdir = tmp_path / "questions"
    _write(qdir, _valid())
    summary, failures = oq.check(qdir, tree)
    assert failures == []
    assert summary[0].startswith("dev: 10 questions (Trace 2, Explain 2, Conflict 2")
    assert summary[1].startswith("screen: 15 questions") and summary[2].startswith("final: 15")


def test_the_split_is_exact_per_kind_and_follows_the_seed() -> None:
    ids = {kind: [f"{kind}{n}" for n in range(8)] for kind in oq.KINDS}
    first = oq.assign_splits(ids, 1)
    assert first == oq.assign_splits(ids, 1)
    for kind, kind_ids in ids.items():
        got = [first[i] for i in kind_ids]
        assert [got.count(s) for s in oq.SPLITS] == [2, 3, 3], kind
    assert any(oq.assign_splits(ids, seed) != first for seed in range(2, 8))


def test_the_split_ignores_input_order() -> None:
    ids = {"Trace": [f"T{n}" for n in range(8)]}
    assert oq.assign_splits(ids, 5) == oq.assign_splits({"Trace": ids["Trace"][::-1]}, 5)


def test_load_returns_typed_questions_and_a_rubric(tmp_path: Path, tree: Path) -> None:
    docs = _valid()
    _write(tmp_path / "questions", docs)
    loaded = oq.load(tmp_path / "questions" / "dev.json")
    assert loaded.split == "dev" and loaded.seed == SEED and len(loaded.questions) == 10
    negative = next(q for q in loaded.questions if q.kind == "Negative")
    rubric = oq.rubric(negative)
    assert rubric.required == ("r1",) and rubric.forbidden == ("f1",) and rubric.no_citation_ok
    assert negative.required[0].search[0].matches == ()


def test_load_names_the_field_that_is_wrong(tmp_path: Path) -> None:
    bad = {"split": "dev", "seed": "7", "questions": []}
    (tmp_path / "dev.json").write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="'seed' must be int"):
        oq.load(tmp_path / "dev.json")


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda q: q["required"][0]["anchors"].__setitem__(0, _anchor("../outside.txt", 1)),
            "not a file inside",
        ),
        (
            lambda q: q["required"][0]["anchors"].__setitem__(0, _anchor("pkg/none.py")),
            "not a file inside",
        ),
        (
            lambda q: q["required"][0]["anchors"].__setitem__(0, _anchor(line=9)),
            "line out of range",
        ),
        (
            lambda q: q["required"][0]["anchors"].__setitem__(0, _anchor(line=0)),
            "line out of range",
        ),
        (
            lambda q: q["required"][0]["anchors"].__setitem__(0, _anchor(text="GAMMA")),
            "text not on its line",
        ),
        (
            lambda q: q["required"][0]["anchors"].__setitem__(0, _anchor(text="")),
            "text not on its line",
        ),
        (
            lambda q: q["required"][0]["anchors"].__setitem__(0, _anchor("pkg/blob.bin", 1)),
            "not readable text",
        ),
        (lambda q: q.__setitem__("forbidden", []), "forbidden is empty"),
        (lambda q: q["forbidden"].append({"id": "f1", "text": "again"}), "ids empty or repeated"),
        (lambda q: q.__setitem__("no_citation_ok", True), "no_citation_ok on a question"),
        (lambda q: q.__setitem__("required", []), "no required facts"),
        (lambda q: q["required"][0].__setitem__("anchors", []), "no anchors"),
        (lambda q: q.__setitem__("kind", "Riddle"), "unknown kind"),
    ],
)
def test_a_bad_anchor_or_shape_is_named(
    tmp_path: Path, tree: Path, mutate: Any, expected: str
) -> None:
    docs = _valid()
    mutate(_first_of(docs, "dev", "Trace"))
    failures = _failures(tmp_path, tree, docs)
    assert any(expected in f and "dev/" in f for f in failures), failures


def test_a_negative_fact_needs_an_anchor_or_a_search(tmp_path: Path, tree: Path) -> None:
    docs = _valid()
    del _first_of(docs, "dev", "Negative")["required"][0]["search"]
    assert any(
        "neither anchors nor a recorded search" in f for f in _failures(tmp_path, tree, docs)
    )


def test_a_locate_all_question_needs_a_recorded_search(tmp_path: Path, tree: Path) -> None:
    docs = _valid()
    del _first_of(docs, "dev", "Locate-all")["search"]
    assert any("no recorded search" in f for f in _failures(tmp_path, tree, docs))


def test_a_recorded_search_is_re_run(tmp_path: Path, tree: Path) -> None:
    docs = _valid()
    question = _first_of(docs, "dev", "Locate-all")
    question["search"][0]["matches"] = ["pkg/mod.py:1"]  # one wrong, one missing
    failures = _failures(tmp_path, tree, docs)
    assert any("recorded matches differ" in f and "pkg/mod.py:4" in f for f in failures)


def test_a_search_that_finds_something_new_fails_the_negative_claim(
    tmp_path: Path, tree: Path
) -> None:
    docs = _valid()
    _first_of(docs, "dev", "Negative")["required"][0]["search"][0]["pattern"] = "alpha"
    assert any("recorded matches differ" in f for f in _failures(tmp_path, tree, docs))


def test_a_pattern_that_does_not_compile_is_reported(tmp_path: Path, tree: Path) -> None:
    docs = _valid()
    _first_of(docs, "dev", "Locate-all")["search"][0]["pattern"] = "("
    assert any("pattern does not compile" in f for f in _failures(tmp_path, tree, docs))


def test_counts_ids_seeds_and_splits_are_checked(tmp_path: Path, tree: Path) -> None:
    docs = _valid()
    dropped = docs["dev"]["questions"].pop()
    failures = _failures(tmp_path, tree, docs)
    assert any(f"dev/{dropped['kind']}: wrong count" in f for f in failures)

    docs = _valid()
    docs["final"]["questions"].append(copy.deepcopy(docs["dev"]["questions"][0]))
    assert any("id repeated" in f for f in _failures(tmp_path, tree, docs))

    docs = _valid()
    docs["screen"]["seed"] = SEED + 1
    assert any("disagree on the seed" in f for f in _failures(tmp_path, tree, docs))

    docs = _valid()
    docs["dev"]["split"] = "final"
    assert any("split field does not match" in f for f in _failures(tmp_path, tree, docs))


def test_a_swap_that_keeps_the_counts_still_breaks_the_seeded_split(
    tmp_path: Path, tree: Path
) -> None:
    docs = _valid()
    a = docs["dev"]["questions"].index(_first_of(docs, "dev", "Explain"))
    b = docs["screen"]["questions"].index(_first_of(docs, "screen", "Explain"))
    dev_q, screen_q = docs["dev"]["questions"][a], docs["screen"]["questions"][b]
    docs["dev"]["questions"][a], docs["screen"]["questions"][b] = screen_q, dev_q
    failures = _failures(tmp_path, tree, docs)
    assert len([f for f in failures if "differs from the seeded shuffle" in f]) == 2


def test_a_missing_or_malformed_file_is_reported(tmp_path: Path, tree: Path) -> None:
    qdir = tmp_path / "questions"
    _write(qdir, _valid())
    (qdir / "final.json").unlink()
    (qdir / "screen.json").write_text("{not json")
    failures = oq.check(qdir, tree)[1]
    assert sum("cannot be read or is malformed" in f for f in failures) == 2


def test_screen_and_final_failures_hide_paths_unless_asked(tmp_path: Path, tree: Path) -> None:
    docs = _valid()
    secret = _first_of(docs, "screen", "Trace")
    secret["text"] = "ZEBRA question text"
    secret["required"][0]["text"] = "ZEBRA fact text"
    secret["required"][0]["anchors"][0] = _anchor("pkg/ZEBRA.py", 3, "ZEBRA anchor text")
    dev = _first_of(docs, "dev", "Trace")
    dev["required"][0]["anchors"][0] = _anchor("pkg/OTTER.py", 3, "x")
    hidden = "\n".join(_failures(tmp_path, tree, docs))
    assert "screen/" in hidden and "ZEBRA" not in hidden
    assert "OTTER" in hidden  # the dev split shows its path
    shown = "\n".join(_failures(tmp_path, tree, docs, detail=True))
    assert "pkg/ZEBRA.py" in shown and "question text" not in shown


def test_search_by_line_and_by_path_skips_git_and_binary_files(tree: Path) -> None:
    walk = oq.Tree(tree)
    everywhere = oq.Search("BETA", ("*",), (), "line", ())
    assert oq.run_search(walk, everywhere) == ["pkg/mod.py:4"]
    named = oq.Search(r"\.md$", ("*",), (), "path", ())
    assert oq.run_search(walk, named) == ["docs/notes.md"]
    excluded = oq.Search("BETA", ("*",), ("pkg/*",), "line", ())
    assert oq.run_search(walk, excluded) == []
    assert walk.lines("pkg/blob.bin") is None
    assert walk.resolve("../outside.txt") is None
    assert walk.resolve("/etc/hostname") is None
    assert not any(p.startswith(".git/") for p in walk.paths())


def test_main_returns_the_exit_status_and_prints_the_outcome(
    tmp_path: Path, tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    qdir = tmp_path / "questions"
    _write(qdir, _valid())
    assert oq.main(["check", "--dir", str(qdir), "--tree", str(tree)]) == 0
    assert capsys.readouterr().out.rstrip().endswith("OK")
    docs = _valid()
    docs["dev"]["questions"].pop()
    _write(qdir, docs)
    assert oq.main(["check", "--dir", str(qdir), "--tree", str(tree)]) == 1
    assert "failure(s)" in capsys.readouterr().out
