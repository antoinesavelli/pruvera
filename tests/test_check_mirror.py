"""The public mirror's acceptance checks: the allowlist, the holdout list and case-blind matching.

Depends on: release/check_mirror.py, bench.issues.{schema,seed}, git, pytest. Every repo, catalogue
and terms file here is synthetic; the one test on the real catalogue asserts counts only.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

from bench.issues import schema, seed
from tests.helpers import git, make_issue

ROOT = Path(__file__).resolve().parents[1]
TERM = "Ézrqvex"  # a made-up private term with an accented capital


@pytest.fixture
def mirror(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Loads release/check_mirror.py as a module; sys.path is restored after the test."""
    monkeypatch.setattr(sys, "path", [*sys.path])
    spec = importlib.util.spec_from_file_location(
        "check_mirror", ROOT / "release" / "check_mirror.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_path_is_allowed_as_a_literal_under_a_directory_or_by_a_regex(mirror: ModuleType) -> None:
    """Proves `is_allowed` keeps exact literals, paths under a `dir/` rule and regex matches."""
    literals, regexes = ["README.md", "policy/", "spikes"], [re.compile(r"^bench/(?!secret\.py$)")]
    assert mirror.is_allowed("README.md", literals, regexes)
    assert mirror.is_allowed("policy/a/b.txt", literals, regexes)
    assert mirror.is_allowed("bench/gate.py", literals, regexes)
    assert not mirror.is_allowed("README.md.bak", literals, regexes)
    assert not mirror.is_allowed("policyx.txt", literals, regexes)
    assert not mirror.is_allowed("spikes/x.py", literals, regexes), "no slash: a file, not a dir"
    assert not mirror.is_allowed("bench/secret.py", literals, regexes)


def test_a_definition_leaks_with_its_id_or_two_planted_lines_but_not_one(
    mirror: ModuleType,
) -> None:
    """Proves `leaks` fires on any strong text or on two planted lines, never on one alone."""
    one, two = b"planted line number one, long enough", b"planted line number two, long enough"
    holdout = [([b"fix-synth01", b"the summary"], [one, two])]
    assert mirror.leaks(b"see fix-synth01 here", holdout)
    assert mirror.leaks(b"x\n" + one + b"\n" + two, holdout)
    assert not mirror.leaks(b"x\n" + one, holdout)
    assert not mirror.leaks(b"nothing", holdout)


def write_catalogue(root: Path, held: list[str]) -> None:
    """A synthetic catalogue: a mined holdout issue, a dev issue and a `holdout3` profile."""
    planted = "return a - b  # planted, long enough"
    for issue in (
        make_issue(id="fix-synth01", summary="mined held out", edits=(edit(planted),)),
        make_issue(id="fix-synth02", summary="dev issue"),
    ):
        schema.write(root / "issues", issue)
    profile = root / "issues" / "profiles" / "holdout3.toml"
    profile.parent.mkdir(parents=True)
    profile.write_text(f'description = "synthetic"\nissues = {json.dumps(held)}\n')


def edit(new: str) -> schema.Edit:
    """An edit of pkg/m.py that plants `new` over a line it replaces."""
    return schema.Edit("pkg/m.py", "return a + b", new)


def test_the_holdout_list_covers_the_profile_mined_issues_and_the_hand_modules(
    mirror: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proves `holdout_texts` takes every `holdout3` id (a mined one, one the catalogue lacks) and
    every hand-module issue, with each one's planted lines, and leaves a dev issue out."""
    write_catalogue(tmp_path, ["fix-synth01", "fix-synth09"])
    hand = make_issue(id="hand-synth03", summary="hand", model_answer="the answer")
    monkeypatch.setattr(mirror, "ROOT", tmp_path)
    monkeypatch.setattr(seed, "generation3_issues", lambda: [hand])
    found = {strong[0]: (strong, planted) for strong, planted in mirror.holdout_texts()}
    assert sorted(found) == [b"fix-synth01", b"fix-synth09", b"hand-synth03"]
    assert found[b"fix-synth01"] == (
        [b"fix-synth01", b"mined held out"],
        [b"return a - b  # planted, long enough"],
    )
    assert found[b"fix-synth09"] == ([b"fix-synth09"], []), "an id the catalogue lacks: id alone"
    assert found[b"hand-synth03"][0] == [b"hand-synth03", b"hand", b"the answer"]


def test_the_holdout_list_is_empty_without_a_catalogue(
    mirror: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proves a copy with no profile, catalogue or hand modules (the public one) yields no
    definitions, so the history check fails closed instead of passing on nothing."""
    monkeypatch.setattr(mirror, "ROOT", tmp_path)
    monkeypatch.setattr(seed, "generation3_issues", lambda: [])
    assert mirror.holdout_texts() == []


def test_the_holdout_list_covers_every_holdout3_issue_of_the_real_catalogue(
    mirror: ModuleType,
) -> None:
    """Proves, by counts only (no id or text is printed), that every `holdout3` id is checked."""
    profile = ROOT / "issues" / "profiles" / "holdout3.toml"
    if not profile.is_file():
        pytest.skip("no catalogue here (the public copy)")
    held = schema.load_profile(profile)[1]
    checked = {strong[0] for strong, _ in mirror.holdout_texts()}
    total, covered = len(held), sum(iid.encode() in checked for iid in held)
    assert total > 0 and covered == total


def make_repo(root: Path, files: dict[str, str], message: str = "init") -> Path:
    """A one-commit repo on `main` holding the given files (UTF-8 text)."""
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")
    git(root, "add", *files)
    git(root, "commit", "-q", "-m", message)
    return root


@pytest.fixture
def release(mirror: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Synthetic release files (terms, mailmap, allowlist) and holdout list for a history check."""
    folder = tmp_path / "release"
    folder.mkdir()
    (folder / "private_terms.local.txt").write_text(TERM + "\n", encoding="utf-8")
    (folder / "mailmap.local.txt").write_text("Pub <pub@example.org> <Quill.Private@Example.org>\n")
    (folder / "allow_paths.txt").write_text("# synthetic\na.txt\nb.txt\n")
    monkeypatch.setattr(mirror, "RELEASE", folder)
    monkeypatch.setattr(mirror, "holdout_texts", lambda: [([b"fix-synth01"], [])])
    monkeypatch.setattr(mirror, "holdout3_subjects", lambda: ["synthsubject"])
    return folder


def test_a_term_or_email_matches_a_blob_in_another_case(
    mirror: ModuleType, release: Path, tmp_path: Path
) -> None:
    """Proves terms and emails match blobs case-blind beyond ASCII: an accented capital in the
    term is found in an upper-case blob, which byte-wise ASCII lowering missed."""
    assert TERM.lower().encode() not in TERM.upper().encode().lower(), "the old comparison"
    repo = make_repo(
        tmp_path / "repo",
        {
            "a.txt": "nothing here",
            "b.txt": f"went to {TERM.upper()}",
            "c.txt": "QUILL.private@EXAMPLE.org",
        },
    )
    hits = mirror.scan_blobs(repo, mirror.every_path(repo))
    assert hits["term"] == ["b.txt"] and hits["email"] == ["c.txt"]


def test_the_history_check_passes_a_clean_mirror_and_fails_a_term_in_a_message(
    mirror: ModuleType, release: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Proves `check_history` passes a clean synthetic mirror, and fails one whose commit message
    holds the term in other case, naming the failed check and never the term."""
    assert mirror.check_history(make_repo(tmp_path / "clean", {"a.txt": "fine"}))
    assert "[FAIL]" not in capsys.readouterr().out
    dirty = make_repo(tmp_path / "dirty", {"a.txt": "fine"}, message=f"about {TERM.upper()}")
    assert not mirror.check_history(dirty)
    out = capsys.readouterr().out
    assert "[FAIL] a. no private term in any message or identity" in out
    assert TERM.casefold() not in out.casefold()
