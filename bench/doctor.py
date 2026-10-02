"""Can every recorded result still be reproduced on this machine? Audit builds, pins and models.

Reads, never writes. For each results file it names the fixture builds, model digests and
opencode versions its records carry and says which of them exist or match now: a result whose
build is gone cannot be rescored, one whose model digest changed was produced by a different
model than `ollama` serves today. Exit status 1 when a build is gone or drifted (`--strict`: any
flag).
Depends on: bench.{layout,modelinfo,runner,jsonl}; a running Ollama for the model digests
(optional).
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from bench import layout, modelinfo, runner
from bench.jsonl import read_jsonl


def _records(path: Path) -> list[dict[str, Any]]:
    return [r for r in read_jsonl(path, skip_bad=True) if "fixture_tree_hash" in r]


def _missing_builds(records: list[dict[str, Any]], version: str) -> list[str]:
    hashes = {r["fixture_tree_hash"] for r in records}
    return sorted(h[:12] for h in hashes if layout.find_profile_dir(h, version) is None)


def _missing_artifacts(records: list[dict[str, Any]]) -> int:
    """Records whose artifact directory (the scorer's input: diff, transcript) is gone."""
    return sum(1 for r in records if not Path(str(r.get("artifact", ""))).is_dir())


def _digests(records: list[dict[str, Any]], current: dict[str, str]) -> tuple[list[str], list[str]]:
    """(models whose digest differs from Ollama's now, models Ollama could not vouch for)."""
    seen: dict[str, set[str]] = {}
    for r in records:
        seen.setdefault(r["model"], set()).add(r.get("model_digest", ""))
    for model in seen:
        current.setdefault(model, runner.model_digest(model))
    changed = [m for m, ds in seen.items() if current[m] and any(d and d != current[m] for d in ds)]
    return sorted(changed), sorted(m for m in seen if not current[m])


def audit_results(results_dir: Path, version: str = layout.VERSION) -> list[dict[str, Any]]:
    """One row per results file: records, missing builds and artifacts, digests that changed."""
    current: dict[str, str] = {}
    rows = []
    for path in sorted(results_dir.rglob("*.jsonl")):
        records = _records(path)
        if not records:
            continue
        changed, unchecked = _digests(records, current)
        rows.append(
            {
                "file": str(path.relative_to(results_dir)),
                "records": len(records),
                "missing_builds": _missing_builds(records, version),
                "missing_artifacts": _missing_artifacts(records),
                "unpinned": sum(
                    1 for r in records if r.get("arm") == "candidate" and not r.get("variant_hash")
                ),
                "model_digest_changed": changed,
                "model_digest_unchecked": unchecked,
                "opencode": sorted({r.get("opencode_version", "") for r in records}),
            }
        )
    return rows


def audit_builds(version: str = layout.VERSION) -> list[str]:
    """Problems with the current profile builds: a tree that drifted from its manifest."""
    problems = []
    for manifest in sorted(layout.version_dir(version).glob("profiles/*/MANIFEST.json")):
        fx = runner.load_profile(version, manifest.parent.name)
        try:
            runner.check_fixture(fx)
        except runner.DriftError as exc:
            problems.append(f"{manifest.parent.name}: {exc}")
    return problems


def _flags(row: dict[str, Any]) -> list[str]:
    flags = []
    if row["missing_builds"]:
        flags.append(f"builds gone: {row['missing_builds']}")
    if row["missing_artifacts"]:
        flags.append(f"{row['missing_artifacts']} artifact dirs gone (cannot be rescored)")
    if row["unpinned"]:
        flags.append(
            f"{row['unpinned']} candidate records carry no variant hash (edits undetectable)"
        )
    if row["model_digest_changed"]:
        flags.append(f"model digest changed: {row['model_digest_changed']}")
    if row["model_digest_unchecked"]:
        flags.append(
            f"model digest UNCHECKED (Ollama unreachable): {row['model_digest_unchecked']}"
        )
    return flags


def _report(rows: list[dict[str, Any]], builds: list[str], builds_checked: bool) -> None:
    clean = "reproducible" if builds_checked else "no flags (builds not re-hashed)"
    for r in rows:
        print(f"{r['file']:50s} {r['records']:4d} records  {'; '.join(_flags(r)) or clean}")
    for p in builds:
        print(f"BUILD DRIFT {p}")
    versions = Counter(v for r in rows for v in r["opencode"] if v)
    print(
        f"opencode versions in records: {dict(versions)}; installed: {modelinfo.opencode_version()}"
    )


def _status(rows: list[dict[str, Any]], builds: list[str], strict: bool) -> int:
    gone = any(r["missing_builds"] for r in rows)
    return 1 if gone or builds or (strict and any(_flags(r) for r in rows)) else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--results", type=Path, default=layout.ROOT / "results")
    ap.add_argument("--skip-builds", action="store_true", help="do not re-hash the profile trees")
    ap.add_argument("--strict", action="store_true", help="exit 1 on any flag, not only on drift")
    args = ap.parse_args(argv)
    rows = audit_results(args.results)
    builds = [] if args.skip_builds else audit_builds()
    _report(rows, builds, not args.skip_builds)
    return _status(rows, builds, args.strict)


if __name__ == "__main__":
    sys.exit(main())
