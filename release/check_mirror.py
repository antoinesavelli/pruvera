"""Acceptance checks for the public mirror (plans/PUBLIC_RELEASE.md Phase 2 step 2), on the output.

Usage, from the private repo root:
    python3 release/check_mirror.py history <mirror>       # a, b, c: terms, paths, credentials
    python3 release/check_mirror.py tests <mirror>         # d: the suite in bwrap, repo not mounted
    python3 release/check_mirror.py same <mirror> <other>  # e: two builds of one commit agree
    python3 release/check_mirror.py review <mirror> [out]  # f: the owner's reading pack
Terms come from release/private_terms.local.txt, the allowlist from release/allow_paths.txt and the
private email from release/mailmap.local.txt (each address after the first on a line). Only counts
and paths are printed, never a matched term. Exit 0 only when every check passes; the build's own
summary is never trusted.
Depends on: the standard library, git, bwrap.
"""

from __future__ import annotations

import argparse
import re
import site
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "release"
CREDENTIALS = [  # shapes that are never a test value: a hit fails the check
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    re.compile(rb"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{32,}\b"),
    re.compile(rb"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(rb"\bAIza[0-9A-Za-z_-]{35}\b"),
]
SECRET_FILES = re.compile(r"(^|/)(\.env(\..*)?|id_rsa|id_ed25519|.*\.(key|pem|p12|pfx))$")


def git(repo: Path, *args: str, data: bytes | None = None) -> bytes:
    run = subprocess.run(
        ["git", "-C", str(repo), *args], input=data, capture_output=True, check=True
    )
    return run.stdout


def lines(path: Path) -> list[str]:
    return [line for line in path.read_text().splitlines() if line]


def terms() -> list[bytes]:
    return [t.lower().encode() for t in lines(RELEASE / "private_terms.local.txt")]


def private_emails() -> list[bytes]:
    """The addresses the mailmap replaces (every <email> after the first on a line)."""
    found = [re.findall(r"<([^>]+)>", line)[1:] for line in lines(RELEASE / "mailmap.local.txt")]
    return [m.encode() for group in found for m in group]


def allowed() -> tuple[list[str], list[re.Pattern[str]]]:
    rules = [r for r in lines(RELEASE / "allow_paths.txt") if not r.startswith("#")]
    regexes = [re.compile(r[6:]) for r in rules if r.startswith("regex:")]
    return [r for r in rules if not r.startswith("regex:")], regexes


def is_allowed(path: str, literals: list[str], regexes: list[re.Pattern[str]]) -> bool:
    return any(path == p or (p.endswith("/") and path.startswith(p)) for p in literals) or any(
        r.match(path) for r in regexes
    )


def every_path(repo: Path) -> dict[str, set[str]]:
    """Blob id -> every path it has in any commit."""
    found: dict[str, set[str]] = {}
    for commit in git(repo, "rev-list", "--all").decode().split():
        for entry in git(repo, "ls-tree", "-r", "-z", commit).decode().split("\0"):
            if entry:
                meta, path = entry.split("\t", 1)
                found.setdefault(meta.split()[2], set()).add(path)
    return found


def blobs(repo: Path, ids: list[str]) -> Iterator[tuple[str, bytes]]:
    raw = git(repo, "cat-file", "--batch", data=("\n".join(ids) + "\n").encode())
    pos = 0
    while pos < len(raw):
        head = raw.index(b"\n", pos)
        sha, _, size = raw[pos:head].decode().split()
        yield sha, raw[head + 1 : head + 1 + int(size)]
        pos = head + 1 + int(size) + 1


def report(name: str, problems: list[str]) -> bool:
    print(
        f"[{'PASS' if not problems else 'FAIL'}] {name}"
        + (f": {len(problems)}" if problems else "")
    )
    for p in sorted(problems)[:40]:
        print(f"         {p}")
    return not problems


Holdout = list[tuple[list[bytes], list[bytes]]]  # per issue: (id, summary, answer), planted lines


def holdout_texts() -> Holdout:
    """What the private holdout (generation 3) must not leak, per issue: its id, summary and model
    answer, and the lines its edits plant. Read from this repo; empty where they are absent."""
    sys.path.insert(0, str(ROOT))
    from bench.issues import seed

    found: Holdout = []
    for issue in seed.generation3_issues():
        strong = [t.encode() for t in (issue.id, issue.summary, issue.model_answer) if t.strip()]
        planted = {
            line.strip().encode()
            for edit in issue.edits
            for line in edit.new.splitlines()
            if line not in set(edit.old.splitlines()) and len(line.strip()) >= 20
        }
        found.append((strong, sorted(planted)))
    return found


def leaks(text: bytes, holdout: Holdout) -> bool:
    """A definition leaks with its id, summary or answer, or two of its planted lines; one planted
    line alone can be generic (a Markdown bullet shared with an older issue)."""
    return any(
        any(t in text for t in strong) or sum(line in text for line in planted) >= 2
        for strong, planted in holdout
    )


def scan_blobs(repo: Path, paths: dict[str, set[str]]) -> dict[str, list[str]]:
    """Blob paths holding a private term, a credential shape, a private email or holdout text."""
    secret_terms, emails, holdout = terms(), [e.lower() for e in private_emails()], holdout_texts()
    hits: dict[str, list[str]] = {"term": [], "credential": [], "email": [], "holdout": []}
    for sha, body in blobs(repo, list(paths)):
        low, where = body.lower(), ", ".join(sorted(paths[sha]))
        if leaks(body, holdout):
            hits["holdout"].append(where)
        if any(t in low for t in secret_terms):
            hits["term"].append(where)
        if any(c.search(body) for c in CREDENTIALS):
            hits["credential"].append(where)
        if any(e in low for e in emails):
            hits["email"].append(where)
    return hits


def check_history(repo: Path) -> bool:
    paths = every_path(repo)
    hits = scan_blobs(repo, paths)
    names = {p for ps in paths.values() for p in ps}
    literals, regexes = allowed()
    messages = git(repo, "log", "--all", "--format=%an <%ae>%n%cn <%ce>%n%B")
    meta, holdout = messages.lower(), holdout_texts()
    in_meta = ["a message or identity"]
    refs = git(repo, "for-each-ref", "--format=%(refname)").decode().split()
    unreachable = git(repo, "fsck", "--unreachable", "--no-reflogs", "--no-progress").decode()
    checks = {
        "a. no private term in any blob of any commit (binary blobs included)": hits["term"],
        "a. no private term in any message or identity": (
            in_meta if any(t in meta for t in terms()) else []
        ),
        "a. no private term in any path": [
            p for p in names if any(t in p.lower().encode() for t in terms())
        ],
        "b. no path outside release/allow_paths.txt in any commit": [
            p for p in names if not is_allowed(p, literals, regexes)
        ],
        "c. no credential-shaped string in any blob": hits["credential"],
        "c. no secret-shaped file name": [p for p in names if SECRET_FILES.search(p)],
        "c. no private email in any blob": hits["email"],
        "c. no private email in any message or identity": (
            in_meta if any(e.lower() in meta for e in private_emails()) else []
        ),
        "refs: only refs/heads/main (no tags, remotes, notes or stash)": (
            [r for r in refs if r != "refs/heads/main"] or ([] if refs else ["no refs at all"])
        ),
        "objects: nothing unreachable in the object store": unreachable.splitlines(),
        "holdout: definitions found to check against": [] if holdout else ["none (fails closed)"],
        "holdout: no text of a generation-3 issue in any blob": hits["holdout"],
        "holdout: no text of a generation-3 issue in any message": (
            in_meta if leaks(messages, holdout) else []
        ),
    }
    ok = [report(name, problems) for name, problems in checks.items()]
    commits = len(git(repo, "rev-list", "--all").split())
    print(f"         ({len(paths)} blobs, {len(names)} paths, {commits} commits)")
    return all(ok)


def bwrap_argv(work: Path) -> list[str]:
    """The suite's sandbox: /usr, /etc and the user's site-packages read-only, the clone at /work.

    Nothing under /mnt is mounted (so neither this repo nor its fixtures), and there is no network.
    The user site-packages is where this machine's pytest lives; it holds tools, not repo content.
    """
    ro = ["/usr", "/etc"]
    tools = Path(site.getusersitepackages())
    if tools.is_dir():
        ro.append(str(tools))
    argv = ["bwrap", "--unshare-all", "--die-with-parent", "--clearenv"]
    argv += [
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/home",
    ]  # before the binds: a later mount would hide them
    for path in ro:
        argv += ["--ro-bind", path, path]
    argv += ["--proc", "/proc", "--dev", "/dev"]
    for link in ("bin", "lib", "lib64", "sbin"):
        argv += ["--symlink", f"usr/{link}", f"/{link}"]
    argv += ["--bind", str(work), "/work", "--chdir", "/work"]
    env = {"PATH": "/usr/bin:/bin", "HOME": "/home/t", "LANG": "C.UTF-8", "TMPDIR": "/tmp"}
    env |= {"PYTHONDONTWRITEBYTECODE": "1", "GIT_CONFIG_NOSYSTEM": "1", "PYTHONPATH": str(tools)}
    for key, value in env.items():
        argv += ["--setenv", key, value]
    return argv


def check_tests(repo: Path) -> bool:
    with tempfile.TemporaryDirectory(prefix="mirror-tests-") as tmp:
        work = Path(tmp) / "work"
        subprocess.run(["git", "clone", "-q", "--no-local", str(repo), str(work)], check=True)
        cmd = ["python3", "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider", "-rfEs"]
        run = subprocess.run(bwrap_argv(work) + cmd, capture_output=True, text=True, timeout=1800)
    out = run.stdout + run.stderr
    tail = [line for line in out.splitlines() if re.match(r"(FAILED|ERROR|SKIPPED) ", line)]
    summary = (re.findall(r"^=*\s*(\d+ (?:passed|failed).*?)\s*=*$", out, re.M) or ["no summary"])[
        -1
    ]
    print(f"         pytest exit {run.returncode}: {summary}")
    if summary == "no summary":
        print("\n".join(f"         | {line}" for line in out.splitlines()[-15:]))
    for line in tail:
        print(f"         {line[:200]}")
    problems = [] if run.returncode == 0 else [summary]
    return report("d. the suite passes in a fresh clone, this repo not mounted", problems)


def check_same(one: Path, two: Path) -> bool:
    a = git(one, "rev-list", "--all").split()
    b = git(two, "rev-list", "--all").split()
    problems = [] if a and a == b else [f"{len(a)} vs {len(b)} commits, or different hashes"]
    return report("e. rebuilding the same source commit gives identical hashes", problems)


def write_review(repo: Path, out: Path, n: int = 10) -> None:
    """Check f's reading pack: the published README, every file at main, n commits spread evenly."""
    commits = git(repo, "rev-list", "--reverse", "main").decode().split()
    picks = sorted(
        {commits[round(i * (len(commits) - 1) / (n - 1))] for i in range(n)}, key=commits.index
    )
    files = [
        line.split(None, 4)
        for line in git(repo, "ls-tree", "-r", "-l", "main").decode().splitlines()
    ]
    show = "--format=commit %H%nAuthor: %an <%ae>%nDate:   %ad%n%n%B"
    parts = [
        "# Public mirror: review pack (release plan Phase 2, check f)",
        "",
        f"Mirror `main` is `{commits[-1][:12]}`: {len(commits)} commits, {len(files)} files. "
        f"Written by `release/check_mirror.py review` from `{repo}`.",
        "",
        "## 1. README.md as published",
        "",
        "````markdown",
        git(repo, "show", "main:README.md").decode().rstrip(),
        "````",
        "",
        "## 2. Every file at main",
        "",
        "| bytes | path |",
        "|---:|---|",
        *(f"| {f[3]} | `{f[4]}` |" for f in files),
        "",
        f"## 3. {len(picks)} commits, spread evenly from the first to the last",
        "",
        "Each shows its message and the files it changed; `git -C <mirror> show <hash>` diffs it.",
        "",
    ]
    for c in picks:
        stat = git(repo, "show", "--stat", "--date=iso", show, c).decode().rstrip()
        parts += ["```", stat, "```", ""]
    out.write_text("\n".join(parts) + "\n")
    print(f"wrote {out}: {len(files)} files, {len(picks)} sampled commits of {len(commits)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("check", choices=("history", "tests", "same", "review"))
    ap.add_argument("mirror", type=Path)
    ap.add_argument("other", type=Path, nargs="?")
    args = ap.parse_args()
    mirror = args.mirror.resolve()
    if ROOT == mirror or ROOT in mirror.parents:
        raise SystemExit("check_mirror: the mirror must be outside this repo")
    if args.check == "history":
        return 0 if check_history(mirror) else 1
    if args.check == "tests":
        return 0 if check_tests(mirror) else 1
    if args.check == "review":
        write_review(mirror, args.other or RELEASE / "REVIEW.local.md")
        return 0
    if args.other is None:
        raise SystemExit("check_mirror: 'same' needs two mirrors")
    return 0 if check_same(mirror, args.other.resolve()) else 1


if __name__ == "__main__":
    sys.exit(main())
