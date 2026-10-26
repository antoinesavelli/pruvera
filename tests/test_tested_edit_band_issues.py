"""The sized tested_edit issues: the properties plans/TESTED_EDIT_BANDS.md pre-registers, pinned.

Depends on: issues/te-band-*, issues/profiles/tested-edit-bands.toml, bench.issues.{schema,tasks}.
Pure reads of the catalogue: no GPU, no fixture tree, no network.
"""

from __future__ import annotations

import re
from pathlib import Path

from bench.issues import schema, tasks
from tests.helpers import needs_catalogue

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE = schema.load_all(ROOT / "issues")
BAND = {i: it for i, it in CATALOGUE.items() if i.startswith("te-band-")}
BOUNDS = {"a": (501, 2000), "b": (2001, 6000)}
LONG_LINE = 12  # a stripped reference line this long must not appear in a prompt
MIN_SITE = 120  # characters of reference text in each edit


def _band(issue_id: str) -> str:
    return issue_id.split("-")[2]


def _named(prompt: str) -> str:
    found = re.findall(r"pytest (\S+) -q", prompt)
    assert len(found) == 1, prompt
    return found[0]


def _goal(prompt: str) -> str:
    return prompt.split("\n\nChange only ")[0]


@needs_catalogue
def test_the_profile_holds_the_band_issues_six_per_band_and_none_is_in_full() -> None:
    _, ids = schema.load_profile(ROOT / "issues/profiles/tested-edit-bands.toml")
    assert set(ids) == set(BAND) and len(ids) == len(set(ids)) == 12
    assert sum(_band(i) == "a" for i in ids) == 6 and sum(_band(i) == "b" for i in ids) == 6
    for name in ("full", "all-kinds", "tested-edit"):
        _, other = schema.load_profile(ROOT / f"issues/profiles/{name}.toml")
        assert not set(other) & set(BAND), name


@needs_catalogue
def test_each_issue_is_one_file_with_two_or_three_sites_in_its_band() -> None:
    for it in BAND.values():
        assert len({e.file for e in it.edits}) == 1, it.id
        assert 2 <= len(it.edits) <= 3, it.id
        assert all(len(e.old) >= MIN_SITE for e in it.edits), it.id
        lo, hi = BOUNDS[_band(it.id)]
        assert lo <= sum(len(e.old) for e in it.edits) <= hi, it.id
        assert it.summary.count(f"reference_chars={sum(len(e.old) for e in it.edits)};") == 1, it.id
        assert all(e.new.strip().startswith("raise NotImplementedError(") for e in it.edits), it.id


@needs_catalogue
def test_each_issue_names_one_test_and_every_hidden_test_has_a_clause() -> None:
    for it in BAND.values():
        named = _named(it.prompt)
        assert named in it.tests and len(it.tests) >= 2 and len(set(it.tests)) == len(it.tests), (
            it.id
        )
        assert it.detector == "test" and it.expected_action == "fix" and it.roles == ("coder",), (
            it.id
        )
        for t in it.tests:
            if t != named:
                assert f"{t.split('::')[-1]}->" in it.summary, (it.id, t)


@needs_catalogue
def test_prompts_follow_the_frame_and_the_goal_rules() -> None:
    for it in BAND.values():
        file = it.edits[0].file
        goal = _goal(it.prompt)
        assert f"Change only `{file}`; do not touch any other file" in it.prompt, it.id
        assert it.prompt.endswith(tasks.REPORT) and "task tool" in it.prompt, it.id
        assert len(goal.split()) <= 90 and "```" not in goal, it.id
        sentences = re.findall(r"[.!?](?:\s|$)", re.sub(r"\([^)]*\)|`[^`]*`", "", goal))
        assert 1 <= len(sentences) <= 3, it.id
        names = re.search(r"names=([^;]*);", it.summary)
        assert names, it.id
        assert all(f"`{n}`" in goal for n in names.group(1).split(",")), it.id
        for e in it.edits:
            for line in (e.old + "\n" + e.new).splitlines():
                stripped = line.strip()
                assert len(stripped) < LONG_LINE or stripped not in it.prompt, (it.id, stripped)


@needs_catalogue
def test_planted_files_and_test_files_are_disjoint_across_the_band_issues() -> None:
    files: dict[str, str] = {}
    tests: dict[str, str] = {}
    for it in BAND.values():
        assert it.edits[0].file not in files, it.id
        files[it.edits[0].file] = it.id
        for t in it.tests:
            path = t.split("::")[0]
            assert tests.setdefault(path, it.id) == it.id, (path, it.id, tests[path])
    for other in CATALOGUE.values():
        if not other.id.startswith("te-band-"):
            assert not {e.file for e in other.edits} & set(files), other.id
