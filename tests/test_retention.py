"""Retention: the report lists, `--apply` deletes only stale overlays and orphan artifacts.

Depends on: bench.{preflight,retention}, pytest.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from bench import layout, preflight, retention

LONG_AGO = time.time() - 5 * 86400


def _dir(path: Path, size: int = 10, old: bool = True) -> Path:
    """A directory holding one file of `size` bytes, aged five days unless `old` is false."""
    path.mkdir(parents=True)
    (path / "f").write_text("x" * size)
    if old:
        os.utime(path, (LONG_AGO, LONG_AGO))
    return path


@pytest.fixture
def state(tmp_path: Path) -> Path:
    """A state tree: one stale and one young overlay, a cited and an orphan artifact, and the
    classes that are only listed."""
    _dir(tmp_path / "overlays" / "stale")
    _dir(tmp_path / "overlays" / "young", old=False)
    _dir(tmp_path / "artifacts" / "cited")
    _dir(tmp_path / "artifacts" / "orphan", size=7)
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "r.jsonl").write_text(json.dumps({"trial_id": "cited"}) + "\n")
    _dir(tmp_path / "runs" / "scratch", size=30)
    (tmp_path / "runs" / "note.log").write_text("y" * 5)
    version = tmp_path / "fixtures" / "paramo" / "versions" / "v2"
    _dir(version / "profiles", size=40)
    _dir(version / "profiles.old-2026-01-01", size=20)
    return tmp_path


def _tree(root: Path) -> set[str]:
    """Every path under `root`, relative to it."""
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def test_the_report_lists_runs_and_old_profiles_with_sizes_and_totals(state: Path) -> None:
    """Runs and old profile sets are listed, the live profiles are not, and the totals add up."""
    found = retention.report(state)
    assert found["runs"] == [
        {"path": "runs/scratch", "bytes": 30},
        {"path": "runs/note.log", "bytes": 5},
    ]
    assert found["old_profiles"] == [
        {
            "path": "fixtures/paramo/versions/v2/profiles.old-2026-01-01",
            "bytes": 20,
            "cited_builds": 0,
        }
    ]
    assert found["bytes"] == {
        "stale_overlays": 10,
        "orphan_artifacts": 7,
        "runs": 35,
        "old_profiles": 20,
        "apply": 17,
        "listed": 72,
    }


def test_the_default_deletes_nothing(
    state: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no flag the module only prints a JSON report of the state it is pointed at."""
    monkeypatch.setattr(layout, "STATE", state)
    before = _tree(state)
    assert retention.main([]) == 0
    assert json.loads(capsys.readouterr().out)["stale_overlays"][0]["path"] == "stale"
    assert _tree(state) == before


def test_apply_removes_only_stale_overlays_and_orphan_artifacts(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A cited artifact, a young overlay, runs/, profiles and results/ survive; the rest goes."""
    assert retention.apply(state) == 0
    out = capsys.readouterr().out
    assert f"deleted {state / 'overlays' / 'stale'}" in out
    assert f"deleted {state / 'artifacts' / 'orphan'}" in out
    assert not (state / "overlays" / "stale").exists()
    assert not (state / "artifacts" / "orphan").exists()
    for kept in ("overlays/young", "artifacts/cited", "runs/scratch", "runs/note.log", "results"):
        assert (state / kept).exists(), kept
    version = state / "fixtures" / "paramo" / "versions" / "v2"
    assert (version / "profiles" / "f").exists()
    assert (version / "profiles.old-2026-01-01" / "f").exists()


def test_apply_refuses_while_a_session_holds_the_lock(state: Path) -> None:
    """A held session lock means nothing is deleted and the exit status is 1."""
    with preflight.session_lock(state / "overlays" / ".session.lock"):
        before = _tree(state)  # the lock file now exists
        assert retention.apply(state) == 1
    assert _tree(state) == before


def test_apply_refuses_without_a_results_directory(state: Path) -> None:
    """No results/ means every artifact looks uncited, so nothing is deleted."""
    (state / "results" / "r.jsonl").unlink()
    (state / "results").rmdir()
    assert retention.apply(state) == 1
    assert (state / "artifacts" / "orphan").exists()


def test_apply_never_follows_a_symlink_out_of_its_directory(
    state: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A symlinked overlay or artifact is skipped, and a link inside a stale overlay is removed
    without touching its target."""
    outside = _dir(tmp_path_factory.mktemp("outside") / "precious")
    (state / "overlays" / "link").symlink_to(outside)
    (state / "artifacts" / "link").symlink_to(outside)
    (state / "overlays" / "stale" / "inner").symlink_to(outside)
    os.utime(state / "overlays" / "stale", (LONG_AGO, LONG_AGO))  # the link bumped its mtime
    assert retention.apply(state) == 0
    assert (outside / "f").read_text() == "x" * 10
    assert (state / "overlays" / "link").is_symlink()
    assert (state / "artifacts" / "link").is_symlink()
    assert not (state / "overlays" / "stale").exists()


def test_apply_restores_permissions_inside_an_overlay_and_removes_its_mode_000_work_dir(
    state: Path,
) -> None:
    """An overlay's `work/work` (mode 000) is deleted, a peer's locked dir elsewhere is not
    touched, and a path that still cannot be removed is reported with exit status 1."""
    work = state / "overlays" / "stale" / "work" / "work"
    work.mkdir(parents=True)
    work.chmod(0o000)
    os.utime(state / "overlays" / "stale", (LONG_AGO, LONG_AGO))
    elsewhere = state / "runs" / "scratch" / "locked"
    elsewhere.mkdir()
    elsewhere.chmod(0o000)
    try:
        assert retention.apply(state) == 0
        assert not (state / "overlays" / "stale").exists()
        assert elsewhere.stat().st_mode & 0o777 == 0
    finally:
        elsewhere.chmod(0o700)


def test_apply_reports_a_path_it_cannot_remove(
    state: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path that cannot be removed is printed to stderr, not hidden, and fails the run."""
    locked = state / "overlays" / "stale" / "work"
    locked.mkdir()
    locked.chmod(0o000)
    os.utime(state / "overlays" / "stale", (LONG_AGO, LONG_AGO))
    monkeypatch.setattr(retention, "_unlock", lambda path: None)
    try:
        assert retention.apply(state) == 1
        assert f"failed {locked}" in capsys.readouterr().err
    finally:
        locked.chmod(0o700)


def test_sizes_count_unique_disk_and_skip_inodes_held_outside(state: Path) -> None:
    """A hard link inside one overlay counts once; a file also held by a fixture or by a
    surviving directory frees nothing; one shared by two stale overlays is counted once."""
    stale = state / "overlays" / "stale"
    os.link(stale / "f", stale / "twin")  # the same 10 bytes twice, inside the overlay
    fixture_file = state / "fixtures" / "paramo" / "versions" / "v2" / "profiles" / "f"
    os.link(fixture_file, stale / "from-fixture")  # 40 bytes also held by the fixture
    kept = state / "overlays" / "young"
    os.link(stale / "f", kept / "keeps-it")  # held by a surviving overlay: frees nothing
    a = _dir(state / "overlays" / "a", size=100)
    b = _dir(state / "overlays" / "b", size=5)
    os.link(a / "f", b / "shared")  # 100 bytes shared by two stale overlays
    for d in (stale, a, b):
        os.utime(d, (LONG_AGO, LONG_AGO))  # adding a link bumped the mtime
    found = retention.report(state)
    sizes = {i["path"]: i["bytes"] for i in found["stale_overlays"]}
    assert sizes == {"stale": 0, "a": 100, "b": 5}
    assert found["bytes"]["stale_overlays"] == 105


def _build(folder: Path, name: str, tree_hash: str | None) -> None:
    """A build `folder/name` whose MANIFEST.json carries `tree_hash` (no manifest when None)."""
    (folder / name).mkdir(parents=True)
    if tree_hash is not None:
        (folder / name / "MANIFEST.json").write_text(json.dumps({"tree_hash": tree_hash}))


def _versions(state: Path) -> Path:
    """The fixture versions directory of `state`."""
    return state / "fixtures" / "paramo" / "versions"


def test_cited_builds_counts_distinct_cited_hashes_the_live_profiles_lack(state: Path) -> None:
    """Of the old folder's builds only a cited hash absent from `profiles/` counts, once even when
    two builds share it; an uncited build, a cited build also in `profiles/`, a bad manifest and a
    missing one count nothing. A scored file and a note are evidence too."""
    version = _versions(state) / "v2"
    old = version / "profiles.old-2026-01-01"
    _build(old, "sole", "h-sole")
    _build(old, "twin", "h-sole")
    _build(old, "shared", "h-shared")
    _build(old, "uncited", "h-uncited")
    _build(old, "nohash", None)
    _build(old, "bad", "x")
    (old / "bad" / "MANIFEST.json").write_text("not json")
    _build(version / "profiles", "live", "h-shared")
    (state / "results" / "r.scored.jsonl").write_text(json.dumps({"fixture_tree_hash": "h-sole"}))
    (state / "results" / "note.md").write_text("ran on h-shared, nothing else.\n")
    (row,) = retention.report(state)["old_profiles"]
    assert row["cited_builds"] == 1


def test_an_old_folder_is_compared_with_its_own_versions_profiles(state: Path) -> None:
    """A v3 folder's hash held only by v2's `profiles/` is still cited-only-here; one held by v3's
    own `profiles/` is not."""
    v2, v3 = _versions(state) / "v2", _versions(state) / "v3"
    _build(v2 / "profiles", "live", "h1")
    _build(v3 / "profiles", "live", "h2")
    old = v3 / "profiles.old-x"
    _build(old, "a", "h1")
    _build(old, "b", "h2")
    (state / "results" / "note.md").write_text("h1 h2\n")
    rows = {r["path"]: r for r in retention.report(state)["old_profiles"]}
    assert rows["fixtures/paramo/versions/v3/profiles.old-x"]["cited_builds"] == 1


@pytest.mark.parametrize(
    "text",
    [
        '{"trial_id": "abc", "outcome": "completed"}\n',
        '{"trial_id": "x"}\n{"trial_id": "abc", "outcome": "comp',  # a torn last line
        '{"row": {"nested": [{"artifact": "abc"}]}}',
        "- artifacts/abc/diff.patch shows the leak\n",
        "see abc.\n",
        '{"trials": ["abc"]}',
    ],
)
def test_every_form_of_mention_cites_a_bare_id(tmp_path: Path, text: str) -> None:
    """A record, a torn line, a nested row, a path, a sentence and a bundle each cite `abc`."""
    (tmp_path / "x.scored.jsonl").write_text(text)
    assert "abc" in retention.cited_trials(tmp_path)


def test_a_longer_name_cites_a_shorter_one_on_purpose(state: Path) -> None:
    """Flipped from "does not cite": the tokens are only a prefilter, and the final substring
    check makes `abc-def` cite `abc`. A false citation only keeps a directory; a guard that
    deletes evidence must err that way."""
    _dir(state / "artifacts" / "abc")
    (state / "results" / "x.txt").write_text('{"trial_id": "abc-def"}\n')
    tokens = retention.cited_trials(state / "results")
    assert "abc-def" in tokens and "abc" not in tokens  # the prefilter alone would delete it
    assert retention.orphan_artifacts(state / "artifacts", state / "results") == ["orphan"]


@pytest.mark.parametrize(
    "text",
    [
        '{"d": "line one\\nabc123"}',  # an id after a JSON-escaped newline
        "\x1b[31mabc123\x1b[0m",
        "ran \u2014abc123\u2014",
        "artifacts/abc123.patch",
        "abc123_diff",
        "id:abc123",
        "trial=abc123",
        "trial abc123 done",
    ],
)
def test_a_substring_mention_keeps_the_artifact(state: Path, text: str) -> None:
    """An id glued to other characters is no token, but still keeps its directory."""
    _dir(state / "artifacts" / "abc123")
    (state / "results" / "x.txt").write_text(text)
    assert retention.orphan_artifacts(state / "artifacts", state / "results") == ["orphan"]


def test_an_id_only_in_a_file_name_keeps_the_artifact(state: Path) -> None:
    """A results file named after the id cites it, even when the file is empty."""
    _dir(state / "artifacts" / "abc123")
    (state / "results" / "abc123.log").write_text("")
    assert "abc123" not in retention.cited_trials(state / "results")
    assert retention.orphan_artifacts(state / "artifacts", state / "results") == ["orphan"]


def test_a_file_that_is_not_text_is_a_problem_and_blocks_apply(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A NUL byte (gzip, UTF-16) means the text may hide ids: listed, `cited_builds` unknown,
    and `--apply` refuses."""
    (state / "results" / "old.jsonl.gz").write_bytes(b"\x1f\x8b\x08\x00binary")
    old = _versions(state) / "v2" / "profiles.old-2026-01-01"
    _build(old, "b", "h1")
    found = retention.report(state)
    assert "NUL byte" in "\n".join(found["evidence_problems"])
    assert [r["cited_builds"] for r in found["old_profiles"]] == [None]
    assert "NUL byte" in _refused(state, capsys)


def test_a_build_behind_a_symlink_in_profiles_does_not_count_as_present(
    state: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A live `profiles/` entry that is only a symlink would vanish with the old folder."""
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    _build(elsewhere, "real", "h1")
    version = _versions(state) / "v2"
    (version / "profiles" / "link").symlink_to(elsewhere / "real")
    _build(version / "profiles.old-2026-01-01", "b", "h1")
    (state / "results" / "note.md").write_text("h1\n")
    (row,) = retention.report(state)["old_profiles"]
    assert row["cited_builds"] == 1


def test_the_report_names_evidence_it_could_not_read_and_carries_on(
    state: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A symlink and an unreadable directory under `results/` are listed; sizes still come."""
    outside = tmp_path_factory.mktemp("outside")
    (state / "results" / "link").symlink_to(outside)
    locked = state / "results" / "locked"
    locked.mkdir()
    locked.chmod(0o000)
    try:
        found = retention.report(state)
    finally:
        locked.chmod(0o700)
    problems = "\n".join(found["evidence_problems"])
    assert "results/link: symlink" in problems and "results/locked" in problems
    assert found["bytes"]["apply"] == 17


def test_the_report_skips_an_unreadable_directory_instead_of_crashing(state: Path) -> None:
    """A directory without search permission inside an overlay is skipped, not a crash."""
    sub = state / "overlays" / "stale" / "r"
    sub.mkdir()
    (sub / "f").write_text("x")
    sub.chmod(0o400)
    os.utime(state / "overlays" / "stale", (LONG_AGO, LONG_AGO))
    try:
        assert retention.report(state)["bytes"]["apply"] == 17
    finally:
        sub.chmod(0o700)


def test_the_apply_total_ignores_inodes_shared_with_runs(state: Path) -> None:
    """A file of a stale overlay also linked from `runs/` frees nothing, so `bytes["apply"]`
    and that overlay's size leave it out."""
    os.link(state / "overlays" / "stale" / "f", state / "runs" / "scratch" / "held")
    found = retention.report(state)
    assert found["stale_overlays"][0]["bytes"] == 0
    assert found["bytes"]["apply"] == 7 == found["bytes"]["orphan_artifacts"]


@pytest.mark.parametrize(
    ("stale", "old", "expected"), [(49, 4, False), (50, 0, True), (0, 5, True)]
)
def test_nudge_fires_at_each_threshold(
    tmp_path: Path, stale: int, old: int, expected: bool
) -> None:
    """The line appears at 50 stale overlays or 5 old profile folders, not below, and names both
    counts and the command."""
    for i in range(stale):
        _dir(tmp_path / "overlays" / f"o{i}")
    for i in range(old):
        _dir(_versions(tmp_path) / "v2" / f"profiles.old-{i}")
    assert retention.nudge(tmp_path) == (
        f"storage: {stale} stale overlays, {old} old profile folders; "
        "list them with python3 -m bench.retention"
        if expected
        else None
    )


def test_nudge_on_an_empty_or_missing_state_is_none(tmp_path: Path, state: Path) -> None:
    """A state with nothing in it, a missing directory and the small fixture state say nothing."""
    assert retention.nudge(tmp_path) is None
    assert retention.nudge(tmp_path / "missing") is None
    assert retention.nudge(state) is None


def test_nudge_never_raises_and_never_scans_or_measures(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OSError while counting gives None; the nudge never builds the report, scans results or
    measures a size."""

    def forbidden(*_a: object, **_k: object) -> None:
        raise AssertionError("the nudge must stay cheap")

    for name in ("report", "_freed", "_scan_results", "cited_trials"):
        monkeypatch.setattr(retention, name, forbidden)
    assert retention.nudge(state) is None

    def broken(*_a: object, **_k: object) -> None:
        raise OSError("no")

    monkeypatch.setattr(retention, "stale_overlays", broken)
    assert retention.nudge(state) is None


def _refused(state: Path, capsys: pytest.CaptureFixture[str]) -> str:
    """Run `apply`, assert it refused with nothing on stdout, and return the stderr reason."""
    assert retention.apply(state) == 1
    captured = capsys.readouterr()
    assert "deleted" not in captured.out and captured.err.startswith("refused:")
    return captured.err


def test_apply_refuses_an_empty_or_public_only_results(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With nothing citing any artifact, nothing is deleted: not overlays, not artifacts."""
    (state / "results" / "r.jsonl").unlink()
    assert "none of 2 artifact" in _refused(state, capsys)
    (state / "results" / "public").mkdir()
    (state / "results" / "public" / "SUMMARY.md").write_text("no ids here\n")
    assert "none of 2 artifact" in _refused(state, capsys)
    for kept in ("overlays/stale", "artifacts/cited", "artifacts/orphan"):
        assert (state / kept).exists(), kept


def test_a_torn_line_still_cites_its_artifact(state: Path) -> None:
    """The artifact named only by a torn last line survives; the real orphan goes."""
    _dir(state / "artifacts" / "torn")
    (state / "results" / "r.jsonl").write_text('{"trial_id": "cited"}\n{"trial_id": "torn", "o')
    assert retention.apply(state) == 0
    assert (state / "artifacts" / "torn").exists() and (state / "artifacts" / "cited").exists()
    assert not (state / "artifacts" / "orphan").exists()


def test_apply_refuses_when_results_cannot_be_fully_read(
    state: Path, capsys: pytest.CaptureFixture[str], tmp_path_factory: pytest.TempPathFactory
) -> None:
    """An unreadable subdirectory or a symlinked one means the evidence may be incomplete."""
    locked = state / "results" / "archive"
    locked.mkdir()
    (locked / "r.jsonl").write_text('{"trial_id": "orphan"}\n')
    locked.chmod(0o000)
    try:
        assert "results/archive" in _refused(state, capsys)
    finally:
        locked.chmod(0o700)
    shutil.rmtree(locked)
    outside = tmp_path_factory.mktemp("outside")
    (outside / "r.jsonl").write_text('{"trial_id": "orphan"}\n')
    (state / "results" / "archive").symlink_to(outside)
    assert "symlink" in _refused(state, capsys)
    assert (state / "artifacts" / "orphan").exists() and (state / "overlays" / "stale").exists()


def test_apply_refuses_a_symlinked_artifacts_or_overlays_directory(
    state: Path, capsys: pytest.CaptureFixture[str], tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A link to another checkout's directory is never deleted through."""
    other = tmp_path_factory.mktemp("other")
    _dir(other / "artifacts" / "t1")
    shutil.rmtree(state / "artifacts")
    (state / "artifacts").symlink_to(other / "artifacts")
    assert "symlink" in _refused(state, capsys)
    assert (other / "artifacts" / "t1" / "f").exists()
    (state / "artifacts").unlink()
    shutil.rmtree(state / "overlays")
    (state / "overlays").symlink_to(other)
    assert "symlink" in _refused(state, capsys)
    assert (other / "artifacts" / "t1" / "f").exists()


@pytest.mark.parametrize(("orphans", "refused"), [(5, False), (6, True)])
def test_apply_refuses_when_over_a_quarter_of_twenty_artifacts_would_go(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], orphans: int, refused: bool
) -> None:
    """A half-restored `results/` makes many artifacts look uncited; 25% is allowed, more is not."""
    (tmp_path / "results").mkdir()
    cited = [f"c{i}" for i in range(20 - orphans)]
    (tmp_path / "results" / "r.jsonl").write_text(
        "".join(f'{{"trial_id": "{c}"}}\n' for c in cited)
    )
    for i in range(20):
        _dir(tmp_path / "artifacts" / (cited + [f"o{i}" for i in range(orphans)])[i])
    if refused:
        assert f"{orphans} of 20" in _refused(tmp_path, capsys)
        assert len(list((tmp_path / "artifacts").iterdir())) == 20
    else:
        assert retention.apply(tmp_path) == 0
        assert len(list((tmp_path / "artifacts").iterdir())) == 15


def test_a_missing_artifacts_or_overlays_directory_is_not_an_error(tmp_path: Path) -> None:
    """With only `results/`, apply has nothing to do and succeeds."""
    (tmp_path / "results").mkdir()
    assert retention.apply(tmp_path) == 0


def test_a_partial_failure_exits_1_and_counts_only_what_was_removed(
    state: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The orphan artifact cannot be removed: its path is on stderr, the status is 1 and the freed
    total is the overlay's 10 bytes alone."""
    real = shutil.rmtree

    def flaky(path: Path, **kw: Any) -> None:
        if Path(path).name == "orphan":
            kw["onexc"](os.rmdir, str(path), PermissionError(13, "denied"))
            return
        real(path, **kw)

    monkeypatch.setattr(retention.shutil, "rmtree", flaky)
    assert retention.apply(state) == 1
    captured = capsys.readouterr()
    assert "freed 10 bytes" in captured.out and "failed" in captured.err
    assert "deleted" in captured.out and "orphan" not in captured.out


def _deep(top: Path, depth: int) -> None:
    """Nest `depth` directories under `top`, deeper than the recursion limit and PATH_MAX."""
    top.mkdir()
    fd = os.open(top, os.O_RDONLY)
    for _ in range(depth):
        os.mkdir("d", dir_fd=fd)
        child = os.open("d", os.O_RDONLY, dir_fd=fd)
        os.close(fd)
        fd = child
    os.close(fd)


def test_apply_survives_a_very_deep_candidate_and_goes_on_to_the_next(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A tree deeper than the recursion limit and PATH_MAX makes `rmtree` give up: that candidate
    is a `failed` line, the status is 1, and the normal candidate after it is still deleted."""
    deep = state / "overlays" / "deep"
    try:
        _deep(deep, 2500)
        os.utime(deep, (LONG_AGO - 86400, LONG_AGO - 86400))  # older, so it goes first
        retention._unlock(deep)  # no ENAMETOOLONG or RecursionError escapes
        assert retention.apply(state) == 1
        captured = capsys.readouterr()
        assert f"failed {deep}" in captured.err
        assert "freed 17 bytes" in captured.out and not (state / "overlays" / "stale").exists()
    finally:
        subprocess.run(["rm", "-rf", str(deep)], check=False)


def test_a_recursion_error_in_one_candidate_is_a_failed_line(
    state: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same, with `rmtree` mocked to raise for the first candidate."""
    real = shutil.rmtree

    def deep(path: Path, **kw: Any) -> None:
        if Path(path).name == "stale":
            raise RecursionError
        real(path, **kw)

    monkeypatch.setattr(retention.shutil, "rmtree", deep)
    assert retention.apply(state) == 1
    captured = capsys.readouterr()
    assert "RecursionError" in captured.err and "freed 7 bytes" in captured.out


def test_a_missing_wrong_type_or_unreadable_directory_never_crashes(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`runs/` at mode 000 is listed as empty; `overlays/` as a file gives no candidates, and
    `apply` refuses (it cannot take the lock) instead of raising; an unreadable `artifacts/`
    is refused, not read as empty."""
    if os.geteuid() == 0:
        pytest.skip("root ignores file modes")
    (state / "runs").chmod(0o000)
    try:
        assert retention.report(state)["runs"] == []
    finally:
        (state / "runs").chmod(0o700)
    (state / "artifacts").chmod(0o000)
    try:
        assert "cannot be listed" in _refused(state, capsys)
        assert retention.report(state)["orphan_artifacts"] == []
    finally:
        (state / "artifacts").chmod(0o700)
    shutil.rmtree(state / "overlays")
    (state / "overlays").write_text("not a directory")
    assert retention.report(state)["stale_overlays"] == []
    assert retention.nudge(state) is None
    _refused(state, capsys)
    assert (state / "artifacts" / "orphan").exists()


def test_apply_refuses_a_symlinked_results_root(
    state: Path, capsys: pytest.CaptureFixture[str], tmp_path_factory: pytest.TempPathFactory
) -> None:
    """`results/` itself a link to somewhere else is not evidence we hold."""
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    (elsewhere / "r.jsonl").write_text('{"trial_id": "cited"}\n')
    shutil.rmtree(state / "results")
    (state / "results").symlink_to(elsewhere)
    assert "symlink" in _refused(state, capsys)
    assert (state / "artifacts" / "orphan").exists()


def test_the_share_brake_comes_before_any_substring_scan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A mass of prefilter orphans is refused as a whole, even if substrings would clear some."""
    (tmp_path / "results").mkdir()
    names = [f"n{i}" for i in range(20)]
    (tmp_path / "results" / "r.txt").write_text(
        "n0 " + " ".join(f"x-{n}" for n in names[1:]) + "\n"
    )  # one token cites n0; the rest only by substring
    for n in names:
        _dir(tmp_path / "artifacts" / n)
    assert "uncited" in _refused(tmp_path, capsys)
    assert len(list((tmp_path / "artifacts").iterdir())) == 20


def test_a_name_json_escapes_is_still_cited(state: Path) -> None:
    """`json.dumps` writes `café-1` as `caf\\u00e9-1`, `a"b` with a backslash: still cited."""
    for name in ("café-1", 'q"uote', "back\\slash"):
        _dir(state / "artifacts" / name)
        with (state / "results" / "r.jsonl").open("a") as fh:
            fh.write(json.dumps({"trial_id": name}) + "\n")
    assert retention.orphan_artifacts(state / "artifacts", state / "results") == ["orphan"]


def test_an_empty_directory_named_after_the_id_cites_it(state: Path) -> None:
    """Directory paths under `results/` are evidence text too."""
    _dir(state / "artifacts" / "abc123")
    (state / "results" / "abc123").mkdir()
    assert retention.orphan_artifacts(state / "artifacts", state / "results") == ["orphan"]


def test_a_closed_stdout_exits_1_without_claiming_a_refusal(
    state: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--apply | head -1` breaks the pipe mid-delete: status 1, no "refused" message."""

    class Closed:
        def write(self, _text: str) -> int:
            raise BrokenPipeError

        def flush(self) -> None:
            raise BrokenPipeError

    monkeypatch.setattr(sys, "stdout", Closed())
    assert retention.apply(state) == 1
    assert "refused" not in capsys.readouterr().err
