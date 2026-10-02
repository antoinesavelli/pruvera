"""Can every recorded result still be reproduced on this machine? Audit builds, pins and models.

Reads, never writes. For each results file it names the fixture builds, model digests and
opencode versions its records carry and says which of them exist or match now: a result whose
build is gone cannot be rescored, one whose model digest changed was produced by a different
model than `ollama` serves today. Exit status 1 when any record cannot be reproduced.
Depends on: bench.{layout,modelinfo,runner}; a running Ollama for the model digests (optional).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from bench import layout, modelinfo, runner


def _records(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text().splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return [r for r in rows if "fixture_tree_hash" in r]


def audit_results(results_dir: Path, version: str = layout.VERSION) -> list[dict[str, Any]]:
    """One row per results file: records, missing builds, models whose digest changed."""
    now: dict[str, str] = {}
    rows = []
    for path in sorted(results_dir.rglob("*.jsonl")):
        records = _records(path)
        if not records:
            continue
        missing = sorted(
            {
                r["fixture_tree_hash"][:12]
                for r in records
                if layout.find_profile_dir(r["fixture_tree_hash"], version) is None
            }
        )
        digests: dict[str, set[str]] = {}
        for r in records:
            digests.setdefault(r["model"], set()).add(r.get("model_digest", ""))
        for model in digests:
            now.setdefault(model, runner.model_digest(model))
        changed = sorted(
            m for m, seen in digests.items() if now[m] and any(d and d != now[m] for d in seen)
        )
        rows.append(
            {
                "file": str(path.relative_to(results_dir)),
                "records": len(records),
                "missing_builds": missing,
                "model_digest_changed": changed,
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--results", type=Path, default=layout.ROOT / "results")
    ap.add_argument("--skip-builds", action="store_true", help="do not re-hash the profile trees")
    args = ap.parse_args(argv)
    rows = audit_results(args.results)
    builds = [] if args.skip_builds else audit_builds()
    for r in rows:
        flags = []
        if r["missing_builds"]:
            flags.append(f"builds gone: {r['missing_builds']}")
        if r["model_digest_changed"]:
            flags.append(f"model digest changed: {r['model_digest_changed']}")
        print(f"{r['file']:50s} {r['records']:4d} records  {'; '.join(flags) or 'reproducible'}")
    for p in builds:
        print(f"BUILD DRIFT {p}")
    versions = Counter(v for r in rows for v in r["opencode"] if v)
    print(
        f"opencode versions in records: {dict(versions)}; installed: {modelinfo.opencode_version()}"
    )
    bad = any(r["missing_builds"] for r in rows) or bool(builds)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
