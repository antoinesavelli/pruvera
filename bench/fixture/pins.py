"""Pin a fixture version's environment: `.git` state in every manifest, venv and data fingerprints.

Without a flag it only reports drift. Run `python -m bench.fixture.pins --version v2 --write` once
after a build and whenever the venv or the data slice is rebuilt on purpose; writing re-blesses
whatever is on disk, so a drifted tree must be understood first. A trial refuses to run if any
pin drifted. Depends on: bench.sandbox (hashes), bench.layout (paths and the data version),
bench.gitutil.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from bench import gitutil, layout, sandbox


def _manifests(version: str) -> list[Path]:
    vdir = layout.version_dir(version)
    return [vdir / "MANIFEST.json", *sorted((vdir / "profiles").glob("*/MANIFEST.json"))]


def current(version: str) -> dict[str, dict[str, str]]:
    """What the pins would be if written now: `git_hash` per manifest, plus the venv and data."""
    return {
        "git_hash": {
            str(m.parent.name): sandbox.git_state_hash(m.parent / "tree")
            for m in _manifests(version)
        },
        "pins": {
            "venv": sandbox.fingerprint(layout.venv(version)),
            "data": sandbox.fingerprint(layout.data_root()),
        },
    }


REPIN_DAYS = 30  # decision-side plan 8.4: cut a new fixture version at least this often


def age(version: str, repo: Path = layout.REAL_REPO) -> dict[str, object]:
    """How far the fixture's source commit is behind the real repo, in days and commits."""
    source = layout.source_commit(version)
    try:
        when = gitutil.text(repo, "show", "-s", "--format=%ct", source)
        behind = gitutil.text(repo, "rev-list", "--count", f"{source}..HEAD")
        rules = gitutil.text(
            repo,
            "diff",
            "--name-only",
            f"{source}..HEAD",
            "--",
            "AGENTS.md",
            "opencode.json",
            ".opencode",
            ".opencode-config",
        )
    except (OSError, ValueError, subprocess.CalledProcessError):
        return {"source_commit": source, "known": False}
    days = int((time.time() - int(when)) / 86400)
    return {
        "source_commit": source[:12], "known": True, "days": days, "commits_behind": int(behind),
        "rule_files_changed": len(rules.split()), "due": days >= REPIN_DAYS,
    }  # fmt: skip


def drift(version: str) -> list[str]:
    """What differs between the recorded pins and the files on disk (empty: nothing drifted)."""
    now = current(version)
    found = []
    for m in _manifests(version):
        recorded = json.loads(m.read_text()).get("git_hash")
        if recorded != now["git_hash"][m.parent.name]:
            found.append(f".git of {m.parent.name}")
    pins_file = layout.version_dir(version) / "PINS.json"
    recorded_pins = json.loads(pins_file.read_text()) if pins_file.exists() else {}
    found += [f"{k} fingerprint" for k, v in now["pins"].items() if recorded_pins.get(k) != v]
    return found


def pin(version: str) -> dict[str, str]:
    """Write `git_hash` into the manifests and PINS.json; returns the pins."""
    now = current(version)
    for path in _manifests(version):
        manifest = json.loads(path.read_text())
        manifest["git_hash"] = now["git_hash"][path.parent.name]
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    (layout.version_dir(version) / "PINS.json").write_text(
        json.dumps(now["pins"], indent=2, sort_keys=True) + "\n"
    )
    return now["pins"]


def main(argv: list[str] | None = None) -> int:
    """Report drift from the recorded pins (exit 1 if any); --write re-pins to what is on disk."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default=layout.VERSION)
    parser.add_argument(
        "--write",
        action="store_true",
        help="re-bless the pins to whatever is on disk NOW (this defeats the drift guard)",
    )
    args = parser.parse_args(argv)
    if not args.write:
        changed = drift(args.version)
        print(json.dumps({"drift": changed}))
        return 1 if changed else 0
    print(json.dumps(pin(args.version)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
