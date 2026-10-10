"""The plan of record cannot fall behind the newest issue-split generation unnoticed.

`issues/profiles/` holds the splits as `tune<N>.toml`, `holdout<N>.toml` and `dev<N>.toml`
(generation 1 has no number). Two rules hold PLAN.md and AGENTS.md to the newest generation that
is committed:

- PLAN.md names every profile of that generation, as a whole word (`dev3` does not satisfy `dev30`);
- PLAN.md's and AGENTS.md's Status dates are not older than the last commit that touched any of
  that generation's profiles (its author date, which a rebase keeps).

The rules are checked on synthetic text, so the checker itself is held to account, and on the repo.
The repo check needs the private catalogue and a real git history, so it skips in the public copy
and in a shallow clone (whose boundary commit carries a date that is not the file's).
"""

from __future__ import annotations

import re
import subprocess
from datetime import date
from pathlib import Path

import pytest

from tests.helpers import needs_catalogue

ROOT = Path(__file__).resolve().parents[1]
PROFILES = "issues/profiles"
SPLIT = re.compile(r"(?:tune|holdout|dev)(\d*)\.toml")
ISO = re.compile(r"\d{4}-\d{2}-\d{2}")
PLAN_STATUS = re.compile(r"\bStatus (\d{4}-\d{2}-\d{2})")


def newest_generation(names: list[str]) -> tuple[int, list[str]]:
    """The highest generation among split profile file names, and its profiles' stems."""
    found = {n: int(m.group(1) or 1) for n in names if (m := SPLIT.fullmatch(n))}
    if not found:
        return 0, []
    newest = max(found.values())
    return newest, sorted(Path(n).stem for n, g in found.items() if g == newest)


def mentions(text: str, stem: str) -> bool:
    """True when `stem` appears as a whole word (`*` and backticks are not word characters)."""
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(stem)}(?![A-Za-z0-9_])", text) is not None


def _day(text: str | None) -> date | None:
    return date.fromisoformat(text) if text else None


def plan_status(plan: str) -> date | None:
    """The date of the first `Status YYYY-MM-DD` in PLAN.md, which is its own header line."""
    match = PLAN_STATUS.search(plan)
    return _day(match.group(1) if match else None)


def agents_status(agents: str) -> date | None:
    """The first date inside AGENTS.md's `## Status` section."""
    section = re.search(r"^## Status\n(.*?)(?=^## |\Z)", agents, re.S | re.M)
    match = ISO.search(section.group(1)) if section else None
    return _day(match.group(0) if match else None)


def stale(plan: str, agents: str, stems: list[str], committed: date) -> list[str]:
    """What is out of date in the plan of record, given the newest generation's commit date."""
    problems = [f"PLAN.md never mentions the profile `{s}`" for s in stems if not mentions(plan, s)]
    for name, found in (("PLAN.md", plan_status(plan)), ("AGENTS.md", agents_status(agents))):
        if found is None:
            problems.append(f"{name} has no dated Status")
        elif found < committed:
            problems.append(
                f"{name} Status is {found}, older than the profiles' commit {committed}"
            )
    return problems


def _git(*args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return done.stdout.strip()


def _committed_profiles() -> list[str]:
    """Profile file names present in HEAD (an index-only or untracked profile is not committed)."""
    listing = _git("ls-tree", "--name-only", "HEAD", f"{PROFILES}/")
    return [Path(line).name for line in (listing or "").splitlines()]


def _last_commit(names: list[str]) -> date | None:
    """The author date of the newest commit that touched any of these profile files."""
    shown = _git("log", "-1", "--format=%as", "HEAD", "--", *(f"{PROFILES}/{n}" for n in names))
    return _day(shown or None)


PLAN_TEXT = "**Status 2026-10-09: built.** Splits `tune3`, `holdout3` and dev3 are frozen."
AGENTS_TEXT = (
    "# x\n\n## Status\n\nBuilt as of 2026-10-09: all.\n\n## Rules\n\nOn 2026-01-01 a thing.\n"
)
DAY = date(2026, 10, 9)


def test_the_newest_generation_is_found_from_file_names() -> None:
    names = [
        "tune.toml",
        "holdout.toml",
        "dev.toml",
        "tune2.toml",
        "holdout2.toml",
        "kind-security.toml",
    ]
    assert newest_generation(names) == (2, ["holdout2", "tune2"])
    assert newest_generation([*names, "dev3.toml", "tune3.toml"]) == (3, ["dev3", "tune3"])
    assert newest_generation(["tune.toml", "full.toml"]) == (1, ["tune"])
    assert newest_generation(["full.toml", "realistic2.toml"]) == (0, [])


def test_a_mention_must_be_the_whole_profile_name() -> None:
    assert mentions("the `dev3` set", "dev3") and mentions("**holdout3**'s look", "holdout3")
    assert not mentions("dev30 and xdev3 and dev3_old", "dev3")


def test_the_status_dates_are_read_from_the_right_place() -> None:
    assert plan_status(PLAN_TEXT) == DAY
    assert plan_status("**Status 2026-10-09: x.** Status 2026-12-01: y.") == DAY
    assert plan_status("no dated status here") is None
    assert agents_status(AGENTS_TEXT) == DAY  # not the 2026-01-01 under Rules
    assert agents_status("# x\n\n## Rules\n\nSince 2026-01-01.\n") is None


def test_a_current_plan_has_no_problems() -> None:
    assert stale(PLAN_TEXT, AGENTS_TEXT, ["dev3", "holdout3", "tune3"], DAY) == []
    assert stale(PLAN_TEXT, AGENTS_TEXT, ["dev3"], date(2026, 10, 8)) == []


def test_a_stale_plan_is_reported_for_each_rule() -> None:
    later = date(2026, 10, 10)
    problems = stale(PLAN_TEXT, AGENTS_TEXT, ["dev3", "tune4"], later)
    assert len(problems) == 3
    assert any("tune4" in p for p in problems)
    assert any(p.startswith("PLAN.md Status") for p in problems)
    assert any(p.startswith("AGENTS.md Status") for p in problems)
    assert stale("no status", "no status", [], DAY) == [
        "PLAN.md has no dated Status",
        "AGENTS.md has no dated Status",
    ]


@needs_catalogue
def test_plan_and_agents_are_not_behind_the_newest_issue_split() -> None:
    if _git("rev-parse", "--is-shallow-repository") != "false":
        pytest.skip("a shallow or absent git history has no trustworthy commit dates")
    generation, stems = newest_generation(_committed_profiles())
    assert generation >= 3 and stems, f"read generation {generation} with profiles {stems}"
    committed = _last_commit([f"{s}.toml" for s in stems])
    assert committed is not None, f"no commit touches {stems}"
    problems = stale(
        (ROOT / "PLAN.md").read_text(), (ROOT / "AGENTS.md").read_text(), stems, committed
    )
    assert problems == [], f"{problems}: update the plan of record for generation {generation}"
