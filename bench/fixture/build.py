"""Build one version of the paramo fixture from a pinned commit, never from a working tree.

Steps: export, exclude, drop dependent tests, add stubs, redact, verify, base commit, manifest.
Depends on: bench.fixture.{export,denylist,droptests,scrub,verify}, bench.{sandbox,gitutil,layout},
git.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from bench import gitutil, layout, sandbox
from bench.fixture import denylist, droptests, export, scrub, verify

HERE = layout.FIXTURES
BASE_DATE = "2026-01-01T00:00:00+0000"  # fixed so the same inputs give the same base commit
HISTORY_CHUNK = 150  # files per synthetic history commit


class BuildError(RuntimeError):
    """The build failed an acceptance check; the tree is left in place for inspection."""


@dataclass
class BuildResult:
    tree: Path
    manifest: dict[str, object]
    report: verify.Report
    leaks: dict[str, int] = field(default_factory=dict)


def _remove(tree: Path, rels: set[str]) -> None:
    for rel in rels:
        target = tree / rel
        if target.is_file() or target.is_symlink():
            target.unlink()
    for path in sorted((p for p in tree.rglob("*") if p.is_dir()), reverse=True):
        if not any(path.iterdir()):
            path.rmdir()


def _copy_stubs(stubs: Path, tree: Path) -> set[str]:
    provided: set[str] = set()
    for src in sorted(p for p in stubs.rglob("*") if p.is_file()):
        rel = src.relative_to(stubs).as_posix()
        dest = tree / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        provided.add(rel)
    return provided


def _scrub_tree(tree: Path, rules: list[scrub.Rule]) -> list[dict[str, object]]:
    changes: list[dict[str, object]] = []
    for path in sorted(p for p in tree.rglob("*") if p.is_file()):
        rel = path.relative_to(tree).as_posix()
        if rel.startswith(".git/"):
            continue
        data = path.read_bytes()
        if not scrub.is_text(data):
            continue
        res = scrub.scrub_text(data.decode("utf-8"), rules, rel.endswith(".py"), rel)
        if res.changes:
            path.write_bytes(res.text.encode("utf-8"))
            changes += [
                {"file": rel, "line": ln, "before_sha256": before, "after": after}
                for ln, before, after in res.changes
            ]
    return changes


def _git_env(tree: Path, date: str) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tree),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_DATE": date,
    }


def history_groups(files: list[str], chunk: int = HISTORY_CHUNK) -> list[tuple[str, list[str]]]:
    """(commit message, files) steps that add every file once: root files, then each directory."""
    top: dict[str, list[str]] = {}
    for rel in sorted(files):
        top.setdefault(rel.split("/", 1)[0] if "/" in rel else "", []).append(rel)
    steps = [("chore: initial import", top.pop(""))] if "" in top else []
    for name, members in sorted(top.items()):
        parts = [members[i : i + chunk] for i in range(0, len(members), chunk)]
        for k, part in enumerate(parts, 1):
            suffix = f" (part {k}/{len(parts)})" if len(parts) > 1 else ""
            steps.append((f"chore({name}): add {name}{suffix}", part))
    return steps


def git_base(tree: Path, history: bool = False) -> str:
    """Give `tree` a fresh repo with fixed author and dates; returns HEAD."""
    # One commit by default. With `history`, the same final tree arrives through one commit per
    # directory (large ones in parts), a day apart, with generic messages: a session then sees a
    # history of realistic depth without any real commit message being copied in.
    env = _git_env(tree, BASE_DATE)
    gitutil.text(tree, "init", "-q", "-b", "main", env=env)
    if not history:
        gitutil.text(tree, "add", "-A", env=env)
        gitutil.text(tree, "commit", "-q", "-m", "fixture base", env=env)
        return gitutil.text(tree, "rev-parse", "HEAD", env=env)
    listing = gitutil.text(tree, "ls-files", "--others", "--exclude-standard", "-z", env=env)
    files = listing.split("\0")
    base = datetime.fromisoformat(BASE_DATE)
    for day, (message, members) in enumerate(history_groups([f for f in files if f])):
        step_env = _git_env(tree, (base + timedelta(days=day)).isoformat())
        gitutil.text(
            tree,
            "add",
            "--pathspec-from-file=-",
            "--pathspec-file-nul",
            env=step_env,
            stdin="\0".join(members),
        )
        gitutil.text(tree, "commit", "-q", "-m", message, env=step_env)
    return gitutil.text(tree, "rev-parse", "HEAD", env=env)


@dataclass
class _Stripped:
    gone: set[str]  # denylisted paths
    excluded_sources: dict[str, str]  # their Python text, kept only to measure identifier leaks
    provided: set[str]  # stub files copied in
    kept: list[str]
    followers: set[str]  # files dropped because they import a removed module


def _strip(tree: Path, paths: list[str], deny: list[str], stubs: Path) -> _Stripped:
    """Remove denylisted paths and their dependents, copy the stubs in."""
    gone = denylist.excluded(paths, deny)
    excluded_sources = {
        p: (tree / p).read_text(errors="replace")
        for p in gone
        if p.endswith(".py") and (tree / p).is_file()
    }
    _remove(tree, gone)
    provided = _copy_stubs(stubs, tree)
    kept = [p for p in paths if p not in gone]
    stub_modules = {m for p in provided if (m := denylist.module_name(p))}
    gone_modules = {m for p in gone if (m := denylist.module_name(p))} - stub_modules
    followers = denylist.dependent_files(tree, kept, gone_modules)
    _remove(tree, followers)
    return _Stripped(gone, excluded_sources, provided, kept, followers)


def _sha(path: Path | None) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path and path.exists() else ""


def build(
    repo: Path,
    rev: str,
    out: Path,
    denylist_path: Path = HERE / "denylist.txt",
    tokens_path: Path = HERE / "scrub_tokens.local.txt",
    stubs: Path = HERE / "stubs",
    allow_path: Path = HERE / "scrub_allow.local.txt",
    drop_path: Path | None = HERE / "drop_tests.txt",
) -> BuildResult:
    """Build `rev` of `repo` into a new version dir `out`; BuildError if built or fails a check."""
    if (out / "MANIFEST.json").exists():
        raise BuildError(
            f"{out} is a built version: fixtures are versioned, never rebuilt in place"
        )
    commit = export.resolve(repo, rev)
    tree = out / "tree"
    paths = export.export_commit(repo, commit, tree)
    deny = denylist.load_rules(denylist_path)
    stripped = _strip(tree, paths, deny, stubs)
    dropped_tests = (
        droptests.drop(tree, droptests.load_ids(drop_path))
        if drop_path is not None and drop_path.exists()
        else []
    )
    rules = scrub.load_rules(tokens_path)
    changes = _scrub_tree(tree, rules)
    allowed = frozenset(allow_path.read_text().split()) if allow_path.exists() else frozenset[str]()
    provided = frozenset(stripped.provided)
    report = verify.check_tree(tree, deny, rules, allowed, provided)
    stub_names = {n for s in provided for n in verify.defined_names((tree / s).read_text())}
    leaks = verify.identifier_leaks(stripped.excluded_sources, tree, stub_names)
    result = BuildResult(tree, {}, report, leaks)
    if not report.ok:
        raise BuildError(
            f"acceptance failed: ciphertext={len(report.ciphertext)} "
            f"denylisted={len(report.denylisted)} residue={len(report.residue)} "
            f"stops={len(report.stops)}"
        )
    manifest: dict[str, object] = {
        "source_commit": commit,
        "fixture_base_commit": git_base(tree),
        "tree_hash": sandbox.tree_hash(tree, (".git",)),  # .git: base commit recorded instead
        "git_hash": sandbox.git_state_hash(tree),  # and its config, hooks and objects, pinned here
        "denylist_sha256": _sha(denylist_path),
        "excluded_paths": len(stripped.gone),
        "dependent_files_dropped": len(stripped.followers),
        "tests_dropped_by_list": len(dropped_tests),
        "drop_list_sha256": _sha(drop_path),
        "stubs": sorted(stripped.provided),
        "redactions": len(changes),
        "kept_paths": len(stripped.kept) - len(stripped.followers) + len(stripped.provided),
        "identifier_leak_names": len(leaks),
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    (out / "redaction_report.json").write_text(json.dumps(changes, indent=1) + "\n")
    (out / "identifier_leaks.json").write_text(json.dumps(leaks, indent=1, sort_keys=True) + "\n")
    result.manifest = manifest
    return result


def main(argv: list[str] | None = None) -> int:
    """Build one fixture version from a repo revision; returns 1 and prints why on a failure."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", type=Path, required=True)
    ap.add_argument("--rev", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--out", type=Path, default=HERE / "versions")
    args = ap.parse_args(argv)
    try:
        result = build(args.repo, args.rev, args.out / args.version)
    except BuildError as exc:
        print(f"BUILD FAILED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result.manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
