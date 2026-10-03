"""The experiment registry: a study registers before its first trial, which spends its holdout look.

An append-only, hash-chained file (`results/registry.jsonl`): each row carries the hash of the row
before it, so an edit or a deletion is found by `verify`. A confirmatory spec in `experiments/` must
be committed before it registers; registering it spends the holdout look of its variant text for
every generation it touches, abandoned or not. A candidate run (a `+variant` profile or a per-role
model override) must name a registered experiment: `authorize` refuses it otherwise and refuses a
run that no longer matches what was registered. `retro` rows record studies that ran before the
registry existed: they spend looks and are never confirmatory. The legacy gate ledger is read as
spent looks too. Depends on: bench.{gitutil,jsonl,layout,ledger}, bench.issues.schema; git.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
import time
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bench import gitutil, layout, ledger
from bench.issues import schema
from bench.jsonl import read_jsonl

KINDS = ("exploratory", "confirmatory", "control")
HOLDOUTS = {"1": "holdout", "2": "holdout2"}  # generation -> the profile holding its issues out
STUDY_DIRS = ("gate", "bakeoff")  # result directories whose files are studies of a candidate
ID = re.compile(r"[a-z0-9][a-z0-9-]{1,60}")


class RegistryError(RuntimeError):
    """The registry refuses this registration, run or edit."""


@dataclass(frozen=True)
class Spec:
    """What a study commits to before its first trial."""

    id: str
    question: str
    kind: str
    baseline: str
    candidate: str
    repeats: int
    decision_rule: str
    models: dict[str, str]


def results_dir(root: Path = layout.ROOT) -> Path:
    """Where results live: the shared state for the real repo, `root/results` for any other."""
    return layout.RESULTS if root == layout.ROOT else root / "results"


def registry_path(root: Path = layout.ROOT) -> Path:
    return results_dir(root) / "registry.jsonl"


def _digest(row: Mapping[str, Any]) -> str:
    body = {k: v for k, v in row.items() if k != "hash"}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def read(root: Path = layout.ROOT) -> list[dict[str, Any]]:
    path = registry_path(root)
    return read_jsonl(path) if path.exists() else []


def verify(rows: list[dict[str, Any]]) -> list[str]:
    """Why the chain is broken (empty when every row links to the one before and hashes true)."""
    problems: list[str] = []
    prev = ""
    for i, row in enumerate(rows):
        if row.get("prev") != prev:
            problems.append(f"row {i + 1}: does not follow row {i} (a row was edited or removed)")
        if row.get("hash") != _digest(row):
            problems.append(f"row {i + 1}: its content does not match its hash")
        prev = str(row.get("hash", ""))
    return problems


def append(root: Path, event: str, **fields: Any) -> dict[str, Any]:
    """Add one row under a lock, after checking the chain it extends."""
    path = registry_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        rows = [json.loads(line) for line in fh if line.strip()]
        if problems := verify(rows):
            raise RegistryError(f"the registry chain is broken: {problems[0]}")
        row: dict[str, Any] = {"event": event, "date": time.strftime("%Y-%m-%d"), **fields}
        row["prev"] = rows[-1]["hash"] if rows else ""
        row["hash"] = _digest(row)
        fh.write(json.dumps(row, sort_keys=True) + "\n")
    return row


def parse_spec(doc: dict[str, Any], stem: str) -> Spec:
    """A spec from its TOML table; the id must be the file's name."""
    missing = [
        k
        for k in ("id", "question", "kind", "baseline", "repeats", "decision_rule")
        if doc.get(k) in (None, "")
    ]
    if missing:
        raise RegistryError(f"{stem}: missing {missing}")
    if doc["id"] != stem or not ID.fullmatch(stem):
        raise RegistryError(
            f"{stem}: the id must equal the file name, lower case letters, digits, dashes"
        )
    if doc["kind"] not in KINDS:
        raise RegistryError(f"{stem}: kind must be one of {KINDS}")
    if not isinstance(doc["repeats"], int) or doc["repeats"] < 1:
        raise RegistryError(f"{stem}: repeats must be a positive integer")
    models = {str(k): str(v) for k, v in dict(doc.get("models", {})).items()}
    return Spec(
        doc["id"], doc["question"], doc["kind"], doc["baseline"], str(doc.get("candidate", "")),
        doc["repeats"], doc["decision_rule"], models,
    )  # fmt: skip


def holdout_issues(root: Path = layout.ROOT) -> dict[str, frozenset[str]]:
    """The issue ids each holdout generation holds out."""
    found: dict[str, frozenset[str]] = {}
    for gen, name in HOLDOUTS.items():
        path = root / "issues" / "profiles" / f"{name}.toml"
        if path.exists():
            found[gen] = frozenset(schema.load_profile(path)[1])
    return found


def profile_gens(root: Path, profile: str) -> set[str]:
    """The holdout generations whose issues a profile plants (clean plants none)."""
    base = ledger.base_of(profile)
    if base == "clean":
        return set()
    path = root / "issues" / "profiles" / f"{base}.toml"
    if not path.exists():
        raise RegistryError(f"no such profile: {base}")
    ids = set(schema.load_profile(path)[1])
    return {gen for gen, held in holdout_issues(root).items() if ids & held}


def _used_by(rows: list[dict[str, Any]], names: set[str], hashes: set[str]) -> set[str]:
    used: set[str] = set()
    for row in rows:
        theirs = row.get("variants", {})
        if (set(theirs) & names) or ({h for h in theirs.values() if h} & hashes):
            used |= set(row.get("holdout_gens", []))
    return used


def _spent(rows: list[dict[str, Any]], root: Path, variants: Mapping[str, str]) -> set[str]:
    """Holdout generations already used by these variant texts (by name or by hash)."""
    names, hashes = set(variants), {h for h in variants.values() if h}
    used = _used_by(rows, names, hashes)
    for e in ledger.read(results_dir(root) / "gate" / "ledger.jsonl"):
        if ledger.variant_of(e["candidate"]) in names or e.get("variant_hash") in hashes:
            used |= ledger.used_gens(e)
    return used


def _committed(spec_path: Path, root: Path) -> tuple[str, str]:
    """(sha256, commit) of a spec that is tracked, unmodified and under `experiments/`."""
    rel = spec_path.resolve().relative_to((root / "experiments").resolve())
    where = f"experiments/{rel.as_posix()}"
    tracked = gitutil.run(root, "ls-files", "--error-unmatch", where, check=False).returncode == 0
    if not tracked or gitutil.text(root, "status", "--porcelain", "--", where):
        raise RegistryError(
            f"{where} must be committed and unmodified: the commit is the preregistration"
        )
    commit = gitutil.text(root, "log", "-1", "--format=%H", "--", where)
    return hashlib.sha256(spec_path.read_bytes()).hexdigest(), commit


def _variants(root: Path, spec: Spec) -> dict[str, str]:
    name = ledger.variant_of(spec.candidate)
    if not name:
        return {}
    vhash = ledger.variant_hash(root / "variants", name)
    if not vhash:
        raise RegistryError(f"variant {name!r} has no files under variants/")
    return {name: vhash}


def _check_kind(spec: Spec, gens: set[str], dirty: bool) -> None:
    if spec.kind != "confirmatory" and gens:
        raise RegistryError(f"{spec.kind} study plants holdout issues (generations {sorted(gens)})")
    if spec.kind == "confirmatory" and not gens:
        raise RegistryError("a confirmatory study must be judged on a holdout generation")
    if spec.kind == "confirmatory" and dirty:
        raise RegistryError("a confirmatory study needs a clean bench/ (commit or stash first)")


def register(spec_path: Path, root: Path = layout.ROOT) -> dict[str, Any]:
    """Register a committed spec; spends the holdout looks it touches."""
    spec = parse_spec(tomllib.loads(spec_path.read_text()), spec_path.stem)
    rows = read(root)
    if any(r["event"] == "registered" and r["id"] == spec.id for r in rows):
        raise RegistryError(f"{spec.id} is already registered")
    sha, commit = _committed(spec_path, root)
    gens = profile_gens(root, spec.baseline) | (
        profile_gens(root, spec.candidate) if spec.candidate else set()
    )
    head, dirty = gitutil.code_state(root, "bench")
    _check_kind(spec, gens, dirty)
    variants = _variants(root, spec)
    if again := _spent(rows, root, variants) & gens:
        raise RegistryError(
            f"{sorted(variants)}: the holdout look for generation(s) {sorted(again)} is spent"
        )
    return append(
        root, "registered", id=spec.id, kind=spec.kind, baseline=spec.baseline,
        candidate=spec.candidate, repeats=spec.repeats, models=spec.models, variants=variants,
        holdout_gens=sorted(gens), spec_sha256=sha, spec_commit=commit, harness_commit=head,
        harness_dirty=dirty,
    )  # fmt: skip


def _registered(rows: list[dict[str, Any]], experiment: str) -> dict[str, Any]:
    mine = [r for r in rows if r.get("id") == experiment]
    found = next((r for r in mine if r["event"] == "registered"), None)
    if found is None:
        raise RegistryError(f"{experiment}: not registered")
    if any(r["event"] == "abandoned" for r in mine):
        raise RegistryError(f"{experiment}: abandoned")
    return found


def registered(experiment: str, root: Path = layout.ROOT) -> dict[str, Any]:
    """The registered row of a live experiment."""
    return _registered(read(root), experiment)


def _check_frozen(
    row: dict[str, Any], profiles: Mapping[str, str], models: Mapping[str, str] | None, root: Path
) -> None:
    """The run must be what was registered: same spec file, profiles, models and variant text."""
    spec_file = root / "experiments" / f"{row['id']}.toml"
    if (
        not spec_file.exists()
        or hashlib.sha256(spec_file.read_bytes()).hexdigest() != row["spec_sha256"]
    ):
        raise RegistryError(f"{row['id']}: the spec changed since it was registered")
    planned = {p for p in (row["baseline"], row["candidate"]) if p}
    if set(profiles.values()) != planned or dict(models or {}) != row["models"]:
        raise RegistryError(f"{row['id']}: the profiles or models differ from the registered ones")
    for name, vhash in row["variants"].items():
        if ledger.variant_hash(root / "variants", name) != vhash:
            raise RegistryError(f"{row['id']}: variant {name!r} changed since it was registered")


def _rel(path: Path, root: Path) -> str:
    """A path relative to the state (or to `root` for any other repo), else as given."""
    base = (layout.STATE if root == layout.ROOT else root).resolve()
    resolved = path.resolve()
    return resolved.relative_to(base).as_posix() if resolved.is_relative_to(base) else str(path)


def authorize(
    experiment: str,
    profiles: Mapping[str, str],
    models: Mapping[str, str] | None,
    out: Path,
    root: Path = layout.ROOT,
) -> str:
    """Let a run start: a candidate run needs a live, unchanged registration. Returns the id."""
    candidate = bool(models) or any(ledger.variant_of(p) for p in profiles.values())
    if not experiment:
        if candidate:
            raise RegistryError(
                "a candidate run (a +variant profile or a model override) needs a registered "
                "experiment: python3 -m bench.experiment run <id>"
            )
        return ""
    rows = read(root)
    row = _registered(rows, experiment)
    _check_frozen(row, profiles, models, root)
    head, dirty = gitutil.code_state(root, "bench")
    if row["kind"] == "confirmatory" and dirty:
        raise RegistryError(f"{experiment}: a confirmatory run needs a clean bench/")
    if not any(r["event"] == "started" and r["id"] == experiment for r in rows):
        append(
            root,
            "started",
            id=experiment,
            out=_rel(out, root),
            harness_commit=head,
            harness_dirty=dirty,
        )
    return experiment


def abandon(experiment: str, reason: str, root: Path = layout.ROOT) -> dict[str, Any]:
    """Close a study without a verdict; its holdout looks stay spent."""
    _registered(read(root), experiment)
    return append(root, "abandoned", id=experiment, reason=reason)


def study_files(root: Path = layout.ROOT) -> list[Path]:
    """The unscored results files of candidate studies."""
    found = (p for d in STUDY_DIRS for p in sorted((results_dir(root) / d).glob("*.jsonl")))
    return [p for p in found if not p.name.endswith(".scored.jsonl") and p.name != "ledger.jsonl"]


def _covered(rows: list[dict[str, Any]], root: Path) -> set[str]:
    out = {str(r.get("out") or r.get("results") or "") for r in rows}
    return out | {
        str(e.get("results", "")) for e in ledger.read(results_dir(root) / "gate" / "ledger.jsonl")
    }


def _study_row(
    path: Path, rel: str, held: dict[str, frozenset[str]], root: Path
) -> dict[str, Any] | None:
    """The `retro` fields of one study file, or None when it holds no trial records."""
    records = [r for r in read_jsonl(path, skip_bad=True) if "trial_id" in r]
    if not records:
        return None
    profiles = {str(r.get("fixture_profile", "")) for r in records}
    names = sorted({ledger.variant_of(p) for p in profiles} - {""})
    labels = {r.get("label") for r in records}
    return {
        "results": rel,
        "trials": len(records),
        "profiles": sorted(profiles),
        "models": sorted(_models(records)),
        "variants": {n: ledger.variant_hash(root / "variants", n) for n in names},
        "holdout_gens": sorted(g for g, ids in held.items() if labels & ids),
        "registered_after_data": True,
    }


def _models(records: list[dict[str, Any]]) -> set[str]:
    return {str(r["model"]) for r in records if r.get("model")}


def retro(root: Path = layout.ROOT) -> list[dict[str, Any]]:
    """Record every study file no row covers, flagged `registered_after_data`; idempotent."""
    held = holdout_issues(root)
    covered = _covered(read(root), root)
    added: list[dict[str, Any]] = []
    for path in study_files(root):
        rel = _rel(path, root)
        fields = None if rel in covered else _study_row(path, rel, held, root)
        if fields:
            added.append(append(root, "retro", **fields))
    return added


def audit(root: Path = layout.ROOT) -> list[str]:
    """Problems: a broken chain, or a candidate study file no row covers."""
    rows = read(root)
    covered = _covered(rows, root)
    orphans = [
        f"{_rel(p, root)}: a candidate study with no registry row"
        for p in study_files(root)
        if _rel(p, root) not in covered
        and any("trial_id" in r for r in read_jsonl(p, skip_bad=True))
    ]
    return [*verify(rows), *orphans]
