"""What can be cleaned up: stale overlays and artifacts no result cites, plus what is only listed.

`python3 -m bench.retention` is read-only and prints a JSON report. Two sets are deletable: a trial
overlay older than `STALE_DAYS` (its trial finished long ago; kept overlays of unusual trials are
the ones worth a look) and an artifact directory whose name appears in no file under `results/`.
Two more are only listed, with sizes, because nothing here knows they are safe to delete: `runs/`
(hand-made scratch with no single writer: legacy-lane run copies from `bin/new-run.sh`, the
red-team study, the grader-audit pack, campaign logs, downloads) and the `profiles.old-*` folders
of a fixture version (superseded profile sets), each with `cited_builds`: the distinct build hashes
in it that a result cites and that version's `profiles/` lacks (a build reached through a symlink
there does not count as present). 0 means removing the folder leaves
`bench.doctor` nothing to flag; above 0 the same hash may also live in another old folder, so one
of them can go, but not all.

What counts as cited fails closed: every file under `results/` (any name, `.scored.jsonl`, bundles,
notes, a torn last line) is split into tokens as a prefilter, and an artifact is cited when its name
is a token or, as a final check, a plain substring of all that text or of any file path there (so
`abc-def` cites `abc`: a false citation only keeps a directory). A build is cited when its hash is
a token. A path under `results/` that cannot be read as text (a NUL byte: gzip, UTF-16), or any
symlink there, is listed as `evidence_problems`, and `cited_builds` is then None. `nudge` is the
cheap one-line reminder that this lister exists.

`--apply` deletes only the two deletable sets, prints each path and the space freed. It refuses
(status 1, deletes nothing, reason on stderr) on anything that could hide a citation:
- a benchmark session holds `bench.preflight.session_lock`, or `overlays/` cannot hold the lock;
- `results/` is missing or a symlink, or any path under it is unreadable, a symlink or not text
  (a NUL byte: gzip, UTF-16);
- `overlays/` or `artifacts/` is a symlink, or `artifacts/` exists but cannot be listed;
- artifacts exist and none is cited, or more than `MAX_ORPHAN_SHARE` of at least
  `MIN_SHARE_ARTIFACTS` artifact directories would go. These two count with the token prefilter
  alone, so they can over-refuse, which fails safe.
It never touches `runs/`, profiles or `results/`, and skips a symlink rather than follow it out
of its directory.
In the read-only report an unreadable directory or file is skipped (its bytes are not counted).
Depends on: bench.{layout,preflight}.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any, NamedTuple

from bench import layout
from bench.preflight import PreflightError, session_lock

STALE_DAYS = 2
NUDGE_STALE_OVERLAYS = 50
NUDGE_OLD_PROFILES = 5  # the ~3 folders that can legitimately stay sit below this line
MAX_ORPHAN_SHARE = 0.25  # `--apply` refuses to delete more than this share of the artifacts
MIN_SHARE_ARTIFACTS = 20  # ... once there are this many (a half-restored `results/`)
YOUNG_SECONDS = 3600  # longer than any trial's wall-clock limit
TOKEN = re.compile(r"[A-Za-z0-9_@+.-]+")
EDGE = ".-_+@"  # punctuation a sentence can glue to an id: "see abc."


def _plain_dir(path: Path) -> bool:
    """True for a real directory (not a symlink); False for anything else, including an error."""
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _ls(root: Path) -> list[Path]:
    """The sorted entries of `root`; nothing when it is missing, not a directory or unreadable."""
    try:
        return sorted(root.iterdir())
    except OSError:
        return []


def _regular_files(root: Path) -> Iterator[os.stat_result]:
    """The `lstat` of each regular file in `root` (a file itself, or a tree; never a symlink)."""
    try:
        top = os.lstat(root)
    except OSError:
        return
    if stat.S_ISREG(top.st_mode):
        yield top
    for d, _, names in os.walk(root) if stat.S_ISDIR(top.st_mode) else ():
        for name in names:
            try:
                info = os.lstat(Path(d) / name)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                yield info


def _freed(paths: list[Path]) -> list[int]:
    """The disk each path frees if all of `paths` are deleted together, in bytes, one per path.

    A regular file counts once per inode, under the first path that holds it, and only when every
    link to the inode is inside `paths`: a file also hard-linked from a fixture, or from a
    directory that stays, frees nothing. Symlinks are not counted or followed; a directory or file
    that cannot be read is skipped (its bytes are unseen).
    """
    inodes: dict[tuple[int, int], list[int]] = {}  # (dev, ino) -> [size, nlink, links seen, owner]
    for index, root in enumerate(paths):
        for info in _regular_files(root):
            entry = inodes.setdefault(
                (info.st_dev, info.st_ino), [info.st_size, info.st_nlink, 0, index]
            )
            entry[2] += 1
    sizes = [0] * len(paths)
    for size, nlink, seen, owner in inodes.values():
        if seen == nlink:
            sizes[owner] += size
    return sizes


def _owned_dirs(root: Path) -> list[Path]:
    """The visible directories directly under `root`; a symlink is not one (it leads elsewhere)."""
    return [p for p in _ls(root) if _plain_dir(p) and not p.name.startswith(".")]


def _kind(path: Path) -> str | None:
    """Why `path` under `results/` is not plain evidence ("symlink", ...), None for a regular file
    or a directory."""
    try:
        mode = os.lstat(path).st_mode
    except OSError as exc:
        return exc.strerror or "unreadable"
    if stat.S_ISLNK(mode):
        return "symlink"
    return None if stat.S_ISREG(mode) or stat.S_ISDIR(mode) else "not a regular file"


def _result_files(results: Path, problems: list[str], names: list[str]) -> Iterator[Path]:
    """The regular files under `results`, archive included; anything else odd goes to `problems`.
    Every entry's path, directories too, is added to `names`."""

    def unreadable(exc: OSError) -> None:
        problems.append(f"{exc.filename}: {exc.strerror}")

    for root, dirs, files in os.walk(results, onerror=unreadable):
        for name in (*dirs, *files):
            path = Path(root) / name
            names.append(str(path.relative_to(results)))
            kind = _kind(path)
            if kind:
                problems.append(f"{path}: {kind}")
            elif name in files:
                yield path


class Evidence(NamedTuple):
    """What `results/` says: `tokens` (the fast prefilter), `text` (all file text and relative file
    paths, for the substring check) and `problems` (paths that could not be read as text)."""

    tokens: set[str]
    text: str
    problems: list[str]

    def mentions(self, name: str) -> bool:
        """True when `name` is a substring of the text, raw or as `json.dumps` escapes it."""
        return name in self.text or json.dumps(name)[1:-1] in self.text


def _scan_results(results: Path) -> Evidence:
    """Read every file under `results` (any name, torn lines included) as `Evidence`.

    A token is a run of `[A-Za-z0-9_@+.-]`, so `artifacts/abc/diff.patch` and `"trial_id": "abc"`
    both give `abc`; each token also counts with edge punctuation cut. A name that is not a token
    can still be a substring of `text`. `problems` holds each unreadable path, each symlink, each
    file with a NUL byte (gzip, UTF-16: not text) and a missing `results`.
    """
    tokens: set[str] = set()
    chunks: list[str] = []
    problems: list[str] = []
    if not os.path.isdir(results):
        return Evidence(tokens, "", [f"{results}: missing"])
    if os.path.islink(results):
        problems.append(f"{results}: symlink")
    for path in _result_files(results, problems, chunks):
        try:
            data = path.read_bytes()
        except OSError as exc:
            problems.append(f"{path}: {exc.strerror}")
            continue
        if b"\0" in data:
            problems.append(f"{path}: not text (NUL byte)")
        text = data.decode("utf-8", errors="replace")
        chunks.append(text)
        tokens.update(TOKEN.findall(text))
    tokens |= {t.strip(EDGE) for t in tokens}
    tokens.discard("")
    return Evidence(tokens, "\n".join(chunks), problems)


def cited_trials(results: Path) -> set[str]:
    """Every token in any file under `results` (the prefilter: see `orphan_artifacts`)."""
    return _scan_results(results).tokens


def _hashes(folder: Path) -> set[str]:
    """The `tree_hash` of each build (`<name>/MANIFEST.json`) in `folder`; a missing or bad
    manifest has none, and a build reached through a symlink does not count (it would vanish
    with the old folder)."""
    found: set[str] = set()
    for manifest in folder.glob("*/MANIFEST.json") if _plain_dir(folder) else ():
        try:
            value = json.loads(manifest.read_text()).get("tree_hash")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(value, str) and _plain_dir(manifest.parent):
            found.add(value)
    return found


def _cited_builds(folder: Path, evidence: Evidence) -> int | None:
    """Distinct build hashes in the old profile `folder` that a result cites and its own version's
    `profiles/` lacks; None when `results/` could not be fully read (0 would be false comfort)."""
    if evidence.problems:
        return None
    return len((_hashes(folder) & evidence.tokens) - _hashes(folder.parent / "profiles"))


def stale_overlays(overlays: Path, now: float | None = None) -> list[dict[str, Any]]:
    """Overlay directories untouched for `STALE_DAYS`, oldest first (hidden names and symlinks are
    skipped)."""
    cutoff = (now or time.time()) - STALE_DAYS * 86400
    found = [
        {"path": p.name, "days": round(((now or time.time()) - p.stat().st_mtime) / 86400, 1)}
        for p in _owned_dirs(overlays)
        if p.stat().st_mtime < cutoff
    ]
    return sorted(found, key=lambda f: -float(f["days"]))


def _uncited_names(artifacts: Path, tokens: set[str], now: float | None = None) -> list[str]:
    """Names of artifact directories that are no token of the evidence; a trial still being
    written (its record is appended last) is skipped, so directories younger than
    `YOUNG_SECONDS` are."""
    cutoff = (now or time.time()) - YOUNG_SECONDS
    return sorted(
        p.name
        for p in _owned_dirs(artifacts)
        if p.name not in tokens and p.stat().st_mtime < cutoff
    )


def orphan_artifacts(
    artifacts: Path, results: Path, now: float | None = None, evidence: Evidence | None = None
) -> list[str]:
    """Artifact directories that no file under `results` mentions. The tokens are a prefilter;
    before a name is an orphan it must also be absent, as a plain substring, from all evidence
    text and file paths. So `abc-def` cites `abc` on purpose: a false citation only keeps a
    directory, and a guard that deletes evidence must err that way."""
    evidence = evidence or _scan_results(results)
    return [n for n in _uncited_names(artifacts, evidence.tokens, now) if not evidence.mentions(n)]


def _listed(paths: list[Path], sizes: list[int], state: Path) -> list[dict[str, Any]]:
    """Each path as `{"path": relative to state, "bytes": size}`, largest first."""
    found = [
        {"path": str(p.relative_to(state)), "bytes": n} for p, n in zip(paths, sizes, strict=True)
    ]
    return sorted(found, key=lambda f: -int(f["bytes"]))


def _old_profile_dirs(state: Path) -> list[Path]:
    """The `profiles.old-*` folders of every fixture version (a symlink is not one)."""
    return [
        p for p in sorted(state.glob("fixtures/paramo/versions/*/profiles.old-*")) if _plain_dir(p)
    ]


def _candidates(
    state: Path, evidence: Evidence
) -> tuple[list[dict[str, Any]], list[str], list[Path], list[Path]]:
    """The stale overlays, orphan artifacts, `runs/` entries and old profile folders of `state`;
    a directory that is missing, a file or unreadable has none."""
    stale = stale_overlays(state / "overlays")
    orphans = orphan_artifacts(state / "artifacts", state / "results", evidence=evidence)
    return stale, orphans, _ls(state / "runs"), _old_profile_dirs(state)


def report(state: Path = layout.STATE) -> dict[str, Any]:
    """The cleanup candidates with sizes, and `bytes`: the total per class.

    `stale_overlays` and `orphan_artifacts` are what `--apply` deletes; `runs` and `old_profiles`
    (each with `cited_builds`, None while `evidence_problems` is non-empty) are only listed.
    `evidence_problems` names every path under `results/` that could not be read as text, and
    every symlink there; the report carries on without it.
    `bytes["apply"]` and the per-overlay sizes are the disk freed by deleting the apply set alone
    (an inode shared with `runs/` or an old profile is not counted); the other classes and
    `bytes["listed"]` are unique disk if everything listed went together (`_freed`), not the sum
    of file sizes. An unreadable directory or file is skipped.
    """
    evidence = _scan_results(state / "results")
    stale, orphans, run_paths, old_paths = _candidates(state, evidence)
    apply_paths = [state / "overlays" / str(i["path"]) for i in stale]
    apply_paths += [state / "artifacts" / n for n in orphans]
    apply_sizes = _freed(apply_paths)
    listed = _freed([*apply_paths, *run_paths, *old_paths])
    run_sizes = listed[len(apply_paths) : len(apply_paths) + len(run_paths)]
    old_sizes = listed[len(apply_paths) + len(run_paths) :]
    for item, size in zip(stale, apply_sizes, strict=False):
        item["bytes"] = size
    old_profiles = _listed(old_paths, old_sizes, state)
    for item in old_profiles:
        item["cited_builds"] = _cited_builds(state / str(item["path"]), evidence)
    return {
        "stale_overlays": stale,
        "orphan_artifacts": orphans,
        "runs": _listed(run_paths, run_sizes, state),
        "old_profiles": old_profiles,
        "evidence_problems": evidence.problems,
        "bytes": {
            "stale_overlays": sum(apply_sizes[: len(stale)]),
            "orphan_artifacts": sum(apply_sizes[len(stale) :]),
            "runs": sum(run_sizes),
            "old_profiles": sum(old_sizes),
            "apply": sum(apply_sizes),
            "listed": sum(listed),
        },
    }


def nudge(state: Path = layout.STATE) -> str | None:
    """One line pointing at this lister when stale overlays or old profile folders pile up, else
    None. Cheap: counts only, no sizes and no results scan; a missing directory counts as zero and
    an unreadable one says nothing."""
    try:
        stale = len(stale_overlays(state / "overlays"))
        old = len(_old_profile_dirs(state))
    except OSError:
        return None
    if stale < NUDGE_STALE_OVERLAYS and old < NUDGE_OLD_PROFILES:
        return None
    return (
        f"storage: {stale} stale overlays, {old} old profile folders; "
        "list them with python3 -m bench.retention"
    )


def _unlock(path: Path) -> None:
    """Give the owner rwx on `path` and every directory below it, so a delete can enter them
    (an overlay's `work/work` is mode 000). Iterative, so a very deep tree cannot overflow the
    stack; symlinks are neither followed nor changed."""

    def chmod(directory: Path) -> None:
        try:
            directory.chmod(directory.stat().st_mode | 0o700)
        except OSError as exc:
            print(f"cannot unlock {directory}: {exc}", file=sys.stderr)

    def failed(exc: OSError) -> None:
        print(f"cannot unlock {exc.filename}: {exc}", file=sys.stderr)

    if not _plain_dir(path):
        return
    chmod(path)
    for root, dirs, _files in os.walk(path, onerror=failed):
        for name in dirs:  # chmod before the walk descends into it
            if not os.path.islink(Path(root) / name):
                chmod(Path(root) / name)


def _precheck(state: Path) -> str | None:
    """Why `apply` must not even take the lock: no `results/`, a symlinked `overlays/` or
    `artifacts/` (the delete would reach into another checkout), or an `artifacts/` that exists
    but cannot be listed (it is not empty)."""
    if not os.path.isdir(state / "results"):
        return f"{state / 'results'} is missing, so no artifact can be called uncited"
    for name in ("overlays", "artifacts"):
        if os.path.islink(state / name):
            return f"{state / name} is a symlink"
    try:
        os.listdir(state / "artifacts")
    except FileNotFoundError:
        pass
    except OSError as exc:
        return f"{state / 'artifacts'} cannot be listed: {exc.strerror}"
    return None


def _refusal(state: Path, evidence: Evidence, uncited: list[str]) -> str | None:
    """Why the evidence is too thin to delete on, else None. `uncited` is the prefilter's list
    (an upper bound on the orphans), so a wiped `results/` is refused before any substring scan."""
    if evidence.problems:
        return (
            f"{len(evidence.problems)} problem(s) reading results/, first: {evidence.problems[0]}"
        )
    names = [p.name for p in _owned_dirs(state / "artifacts")]
    if names and not any(n in evidence.tokens for n in names):
        return f"none of {len(names)} artifact directories is cited under results/"
    if len(names) >= MIN_SHARE_ARTIFACTS and len(uncited) > MAX_ORPHAN_SHARE * len(names):
        return (
            f"{len(uncited)} of {len(names)} artifact directories are uncited (is results/ whole?)"
        )
    return None


def _guarded(path: Path, action: Callable[[Path], object], failed: list[str]) -> bool:
    """Run `action(path)`; an `OSError` or `RecursionError` (a very deep tree) becomes a `failed`
    line for `path`, so the next candidate still gets its turn."""
    try:
        action(path)
    except (OSError, RecursionError) as exc:
        failed.append(f"{path}: {exc!r}")
        return False
    return True


def _remove(path: Path, failed: list[str]) -> bool:
    """`shutil.rmtree` that records every path it cannot remove; True when nothing failed."""
    before = len(failed)

    def note(_func: object, failed_path: str, exc: BaseException) -> None:
        failed.append(f"{failed_path}: {exc}")

    return _guarded(path, lambda p: shutil.rmtree(p, onexc=note), failed) and len(failed) == before


def _delete(paths: list[Path]) -> tuple[int, list[str]]:
    """Remove each path, printing it with its size; returns the bytes of the fully removed ones
    and one message per path that could not be removed."""
    failed = list[str]()
    ready = [p for p in paths if _guarded(p, _unlock, failed)]
    sizes = dict(zip(paths, _freed(paths), strict=True))
    freed = 0
    for path in ready:
        if _remove(path, failed):
            freed += sizes[path]
            print(f"deleted {path} ({sizes[path]} bytes)")
    return freed, failed


def _plan(state: Path) -> tuple[list[Path], str | None]:
    """The directories `apply` would delete now, and why it must not (None when it may)."""
    evidence = _scan_results(state / "results")
    uncited = _uncited_names(state / "artifacts", evidence.tokens)
    if why := _refusal(state, evidence, uncited):
        return [], why
    found = [state / "overlays" / str(i["path"]) for i in stale_overlays(state / "overlays")]
    found += [state / "artifacts" / n for n in uncited if not evidence.mentions(n)]
    return [p for p in found if _plain_dir(p)], None


def apply(state: Path = layout.STATE) -> int:
    """Delete the stale overlays and orphan artifacts, printing each path and the space freed.
    A closed stdout (`| head`) ends it with status 1 and no message.

    Refuses (returns 1, deletes nothing, reason on stderr) in the cases the module docstring
    lists. A candidate that is a symlink, or no longer a plain directory directly under its
    parent, is never removed. Owner permissions are restored inside each candidate only; every
    path that still cannot be removed is printed to stderr and makes the exit status 1.
    """
    try:
        return _apply(state)
    except BrokenPipeError:
        return 1


def _apply(state: Path) -> int:
    """The body of `apply`."""
    if why := _precheck(state):
        print(f"refused: {why}", file=sys.stderr)
        return 1
    with ExitStack() as stack:
        try:
            stack.enter_context(session_lock(state / "overlays" / ".session.lock"))
        except (PreflightError, OSError) as exc:  # a held lock, or an unusable `overlays/`
            print(f"refused: {exc}", file=sys.stderr)
            return 1
        paths, why = _plan(state)
        if why:
            print(f"refused: {why}", file=sys.stderr)
            return 1
        freed, failed = _delete(paths)
    for line in failed:
        print(f"failed {line}", file=sys.stderr)
    print(f"freed {freed} bytes ({freed / 1e9:.2f} GB)")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    """Print the retention report as JSON, or with `--apply` delete the two deletable sets."""
    parser = argparse.ArgumentParser(prog="python3 -m bench.retention", description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="delete stale overlays and orphan artifacts (never runs/, profiles or results/)",
    )
    args = parser.parse_args(argv)
    if args.apply:
        return apply(layout.STATE)
    print(json.dumps(report(layout.STATE), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
