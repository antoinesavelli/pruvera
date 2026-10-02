"""Gaps in the catalogue tools: seeding, area acceptance, drops, restore, layout, rag index."""

from __future__ import annotations

import dataclasses
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from bench import layout, sandbox
from bench.fixture import build, droptests, scrub
from bench.issues import areas, plant, restore, schema, seed, verify
from tests.helpers import make_issue

np = pytest.importorskip("numpy")
from bench.rag import index  # noqa: E402


def test_seed_writes_the_catalogue_profiles_and_splits_into_a_temporary_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "fixtures/paramo/versions/v2/tree").mkdir(parents=True)
    (tmp_path / "issues").mkdir()
    (tmp_path / "issues/_campaign.json").write_text("[]")
    hands = seed.hand_issues()
    extra = [
        dataclasses.replace(hands[0], id=i)
        for i in ("mut-trading_calendar-145", "mut-effective_n-53")
    ]
    mined = [dataclasses.replace(hands[0], id=f"fix-{i}") for i in ("0cb0835b", "c0d8b8e4")]
    monkeypatch.setattr(seed, "CAUGHT", [])
    monkeypatch.setattr(seed, "SURVIVING", [])
    monkeypatch.setattr(seed, "hand_issues", lambda: hands + extra + mined)
    seeded = seed.seed(tmp_path)
    assert len(seeded) == len(hands) + 4
    prof = tmp_path / "issues/profiles"
    names = {p.stem for p in prof.glob("*.toml")}
    assert {"full", "tune", "holdout", "tune2", "holdout2", "dev", "reverted-fixes"} <= names
    assert "realistic2" in names, "its eight issues are all in the catalogue"
    held = [set(schema.load_profile(prof / f"{n}.toml")[1]) for n in ("holdout", "holdout2")]
    assert not held[0] & held[1], "the second holdout is drawn from what the first left"
    before = (prof / "holdout.toml").read_text()
    seed.seed(tmp_path)
    assert (prof / "holdout.toml").read_text() == before, "the first split is frozen once written"


def test_the_seed_command_writes_only_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert seed.main([]) == 0 and "dry run" in capsys.readouterr().out
    monkeypatch.setattr(layout, "ROOT", tmp_path)
    monkeypatch.setattr(seed, "seed", lambda root: [make_issue()])
    assert seed.main(["--write"]) == 0
    assert "1 seeded issues written" in capsys.readouterr().out


def test_the_areas_command_prints_and_optionally_writes_the_accepted_issues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/m.py").write_text("def f(x):\n    if x > 5:\n        return 1\n    return 0\n")
    mutant = {"line": 2, "col": 9, "old": ">", "new": ">=", "operator": "flip", "killed": True}
    mutant["failed"] = ["tests/test_m.py::test_f"]
    rows = [{"module": "pkg/m.py", "baseline_passed": True, "mutants": [mutant]}]
    campaign = tmp_path / "campaign.json"
    campaign.write_text(json.dumps(rows))
    monkeypatch.setattr(layout, "tree", lambda: tmp_path)
    monkeypatch.setattr(layout, "ROOT", tmp_path)
    assert areas.main([str(campaign)]) == 0
    assert "mut-m-2" in capsys.readouterr().out and not (tmp_path / "issues").exists()
    assert areas.main([str(campaign), "--write"]) == 0
    assert (tmp_path / "issues/mut-m-2/issue.toml").exists()


def test_a_drop_file_lists_ids_and_a_drop_that_breaks_the_file_is_refused(tmp_path: Path) -> None:
    listing = tmp_path / "drop.txt"
    listing.write_text("# comment\n\ntests/a.py::test_x  # why\ntests/a.py::C::test_y\n")
    assert droptests.load_ids(listing) == ["tests/a.py::test_x", "tests/a.py::C::test_y"]
    tree = tmp_path / "tree"
    (tree / "tests").mkdir(parents=True)
    (tree / "tests/a.py").write_text("class C:\n    def test_y(self):\n        pass\n")
    with pytest.raises(droptests.DropError, match="does not parse"):
        droptests.drop(tree, ["tests/a.py::C::test_y"])
    assert "test_y" in (tree / "tests/a.py").read_text(), "a refused drop writes nothing"
    with pytest.raises(droptests.DropError, match="file missing"):
        droptests.drop(tree, ["tests/none.py::test_x"])


def test_code_comparison_ignores_comments_in_python_and_survives_broken_tokens() -> None:
    assert restore._code_only("x = 1  # note\n\n", "m.py") == "x = 1\n\n"
    assert restore._code_only('s = """open\n', "m.py").startswith("s =")
    assert restore._holds("a: 1\nb: 2\n", "a: 1", "a: 9", "cfg.yaml")
    assert not restore._holds("a: 9\n", "a: 1", "a: 9", "cfg.yaml")


def test_verify_helpers_apply_text_edits_and_name_tests_that_mention_a_module(
    tmp_path: Path,
) -> None:
    edit = schema.Edit("m.py", "old", "new")
    assert verify._apply_text("x old y", edit) == "x new y"
    assert verify._apply_text("old", schema.Edit("m.py", "old", "")) == ""
    with pytest.raises(plant.PlantError, match="2 times"):
        verify._apply_text("old old", edit)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_m.py").write_text("import mod_x\n")
    env: Any = type("E", (), {"tree": tmp_path})()
    issue = make_issue(edits=(schema.Edit("pkg/mod_x.py", "a", "b"),))
    assert "test_m.py" in verify._check_coverage(env, issue)[0]
    assert (
        verify._check_coverage(env, make_issue(edits=(schema.Edit("pkg/other.py", "a", "b"),)))
        == []
    )


def test_a_profile_is_found_by_its_tree_hash_among_current_and_superseded_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, h in (("profiles", "aaa"), ("profiles.old-1", "bbb")):
        manifest = tmp_path / name / "full" / "MANIFEST.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"tree_hash": h}))
    monkeypatch.setattr(layout, "version_dir", lambda version=layout.VERSION: tmp_path)
    assert layout.find_profile_dir("bbb") == tmp_path / "profiles.old-1" / "full"
    assert layout.find_profile_dir("zzz") is None


def test_the_docs_index_chunks_markdown_embeds_it_and_writes_two_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tree = tmp_path / "tree"
    (tree / "docs").mkdir(parents=True)
    (tree / "docs/a.md").write_text("x" * (index.CHUNK + 5))
    (tree / "docs/b.py").write_text("ignored")
    (tree / "tests/golden").mkdir(parents=True)
    (tree / "tests/golden/c.md").write_text("skipped")
    seen: list[int] = []

    def embed(texts: list[str]) -> Any:
        seen.append(len(texts))
        return np.ones((len(texts), 2), dtype=np.float32)

    monkeypatch.setattr(index, "embed_documents", embed)
    counts = index.build(tree, tmp_path / "out")
    assert counts == {"chunks": 2, "files": 1} and seen == [2]
    assert json.loads((tmp_path / "out/chunks.json").read_text())[0]["file"] == "docs/a.md"
    assert np.load(tmp_path / "out/vectors.npy").shape == (2, 2)
    monkeypatch.setattr(layout, "tree", lambda v=layout.VERSION: tree)
    monkeypatch.setattr(layout, "rag_dir", lambda v=layout.VERSION: tmp_path / "out2")
    assert index.main([]) == 0 and '"chunks": 2' in capsys.readouterr().out


def test_scrubbing_skips_git_internals_and_binary_files(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git/config").write_text("secret")
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01secret")
    (tmp_path / "a.txt").write_text("hello secret\n")
    rule = scrub.Rule("G", None, re.compile("secret"), "REDACTED")
    changes = build._scrub_tree(tmp_path, [rule])
    assert (tmp_path / ".git/config").read_text() == "secret"
    assert (tmp_path / "blob.bin").read_bytes() == b"\x00\x01secret"
    assert "REDACTED" in (tmp_path / "a.txt").read_text() and changes


def test_the_build_command_prints_a_manifest_or_reports_the_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["--repo", str(tmp_path), "--rev", "HEAD", "--version", "v9", "--out", str(tmp_path)]
    result = build.BuildResult(tmp_path, {"tree_hash": "abc"}, None)  # type: ignore[arg-type]
    monkeypatch.setattr(build, "build", lambda repo, rev, out: result)
    assert build.main(argv) == 0 and '"tree_hash": "abc"' in capsys.readouterr().out

    def fail(repo: Path, rev: str, out: Path) -> None:
        raise build.BuildError("leak found")

    monkeypatch.setattr(build, "build", fail)
    assert build.main(argv) == 1 and "BUILD FAILED: leak found" in capsys.readouterr().err


def test_a_reference_copy_is_a_one_commit_repo_with_a_saved_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bench import reference

    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": "/dev/null", "HOME": str(tmp_path)}
    ident = ["-c", "user.name=t", "-c", "user.email=t@t"]
    (repo / "a.txt").write_text("one\n")
    for args in (["init", "-q", "-b", "main"], ["add", "a.txt"], [*ident, "commit", "-qm", "c1"]):
        subprocess.run(["git", "-C", str(repo), *args], env=env, check=True)
    monkeypatch.setattr(reference, "REAL_VENV", tmp_path / "no-venv")
    tree = reference.prepare(repo, "HEAD", tmp_path / "ref")
    assert (tree / "a.txt").read_text() == "one\n"
    manifest = json.loads((tmp_path / "ref/manifest.json").read_text())
    assert manifest["fixture_base_commit"] and "venv_pin" not in manifest
    head = subprocess.run(
        ["git", "-C", str(tree), "rev-list", "--count", "HEAD"], capture_output=True, text=True
    )
    assert head.stdout.strip() == "1"
    monkeypatch.setattr(reference, "REAL_VENV", tmp_path)
    monkeypatch.setattr(sandbox, "fingerprint", lambda p: "fp")
    reference.prepare(repo, "HEAD", tmp_path / "ref2")
    assert json.loads((tmp_path / "ref2/manifest.json").read_text())["venv_pin"] == "fp"
