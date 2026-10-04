"""The front door for studies: write a spec, register it, run it, abandon it, audit the registry.

`new` writes a spec template under `experiments/`; once it is committed, `register` makes it the
preregistration; `run` runs exactly what was registered (the only way to run a candidate) and
writes `results/experiments/<id>.jsonl`; `interim` and `judge` apply the registered alpha share
and write a verdict bundle (`results/verdicts/<id>/`) with a generated report.
Depends on: bench.{budget,gate,jsonl,layout,ledger,registry,report,retention,runner},
bench.issues.trials.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from bench import budget, gate, layout, ledger, registry, report, retention, runner
from bench.issues import trials
from bench.jsonl import read_jsonl

TEMPLATE = """\
id = "{id}"
question = ""  # one sentence: what would change if the answer is yes
kind = "exploratory"  # exploratory (no holdout issues) | confirmatory (holdout) | control
baseline = "dev"
candidate = ""  # "dev+<variant>"; leave empty for a baseline-only study
repeats = 6
decision_rule = ""  # the verdict rule and thresholds, fixed before the first trial
# interim = [2, 4]  # planned interim looks, after these repeat counts (confirmatory)
# safety_repeats = 12  # repeats of each safety issue when larger than `repeats`
# safety_counts = "landed"  # "all" (default) also counts a blocked attempt as unsafe
# underpowered = ""  # the reason, if the registration power check says the design cannot decide
# [models]  # per-role model overrides for the candidate arm, e.g. git = "granite4.1:8b"
"""


def new(experiment_id: str, root: Path = layout.ROOT) -> Path:
    """Write a spec template; refuses to overwrite one."""
    if not registry.ID.fullmatch(experiment_id):
        raise registry.RegistryError(f"{experiment_id!r}: lower case letters, digits and dashes")
    path = root / "experiments" / f"{experiment_id}.toml"
    if path.exists():
        raise registry.RegistryError(f"{path.name} exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE.format(id=experiment_id))
    return path


def run(
    experiment_id: str,
    out: Path | None = None,
    *,
    n: int | None = None,
    only: list[str] | None = None,
    force: bool = False,
    root: Path = layout.ROOT,
) -> Path:
    """Run the registered experiment: both arms, the registered repeats, the registered models."""
    row = registry.registered(experiment_id, root)
    if row["kind"] == "confirmatory" and (n or only or out):
        raise registry.RegistryError(
            f"{experiment_id}: a confirmatory run is exactly what was registered "
            "(no --n, --only or --out)"
        )
    arms = {"baseline": runner.load_profile(layout.VERSION, row["baseline"])}
    if row["candidate"]:
        arms["candidate"] = runner.load_profile(layout.VERSION, row["candidate"])
        if arms["baseline"].issue_ids != arms["candidate"].issue_ids:
            raise ValueError("the two profiles plant different issues: the arms are not comparable")
    target = out or _results(experiment_id, root)
    return trials.run_arms(
        arms, n or row["repeats"], target, only=only, models=row["models"] or None,
        force=force, experiment=experiment_id,
    )  # fmt: skip


def _alpha_fields(spec: registry.Spec, gens: set[str], root: Path) -> dict[str, Any]:
    """What a registration adds: the gate's thresholds, and for a confirmatory study its alpha
    share, look levels and power check."""
    if spec.kind != "confirmatory":
        return {"gate_rules": gate.rules()}
    alpha = budget.alpha_for(registry.read(root), gens, root)
    result = budget.power(spec, alpha, root)
    if not budget.adequate(result) and not spec.underpowered:
        raise registry.RegistryError(
            f"{spec.id}: the design clears a harmless rule {result['clear_if_harmless']:.0%} of "
            f"the time at alpha {alpha:.4f} (needs {budget.POWER_MIN:.0%}); enlarge it, or state "
            'why it is run anyway with underpowered = "<reason>"'
        )
    levels = budget.look_levels(spec.repeats, spec.interim, alpha)
    return {
        "alpha": alpha,
        "power": result,
        "look_levels": {str(k): v for k, v in levels.items()},
        "gate_rules": gate.rules(),
    }


def register(spec_path: Path, root: Path = layout.ROOT) -> dict[str, Any]:
    """Register a spec; a confirmatory one also gets its alpha share and a power check."""
    return registry.register(spec_path, root, lambda spec, gens: _alpha_fields(spec, gens, root))


def _results(experiment_id: str, root: Path) -> Path:
    return registry.results_dir(root) / "experiments" / f"{experiment_id}.jsonl"


def _arms(
    experiment_id: str, root: Path
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    row = registry.registered(experiment_id, root)
    if not row["candidate"]:
        raise registry.RegistryError(f"{experiment_id}: a baseline-only study has nothing to judge")
    scored = gate.score_arms(_results(experiment_id, root), row["baseline"], row["candidate"])
    return row, gate.arm_rows(scored, "baseline"), gate.arm_rows(scored, "candidate")


def _through(rows: list[dict[str, Any]], repeat: int) -> list[dict[str, Any]]:
    return [r for r in rows if (r.get("repeat") or 0) <= repeat]


def interim(experiment_id: str, after: int, root: Path = layout.ROOT) -> dict[str, Any]:
    """A planned interim look: STOP if the success or safety test already rejects, else CONTINUE.

    Each look is taken once and recorded, so a STOP cannot be quietly re-rolled.
    """
    row, base, cand = _arms(experiment_id, root)
    levels = budget.look_levels(row["repeats"], tuple(row["interim"]), row.get("alpha", 0.05))
    if after not in levels or after == row["repeats"]:
        raise registry.RegistryError(f"{experiment_id}: no planned interim look after {after}")
    if any(
        r["event"] == "interim" and r["id"] == experiment_id and r["after"] == after
        for r in registry.read(root)
    ):
        raise registry.RegistryError(f"{experiment_id}: the look after {after} was already taken")
    verdict = gate.decide(
        _through(base, after),
        _through(cand, after),
        family=budget.family_for(levels[after]),
        counts=row.get("safety_counts", "all"),
    )
    decision = "STOP" if verdict["verdict"] == "REJECT" else "CONTINUE"
    registry.append(
        root, "interim", id=experiment_id, after=after, alpha=levels[after], decision=decision
    )
    return {"after": after, "alpha": levels[after], "decision": decision,
            "why": verdict["why"], "success": verdict["success"]}  # fmt: skip


def _scope(records: list[dict[str, Any]]) -> dict[str, Any]:
    """What a verdict holds for: models and their digests, opencode, fixture source, rule text."""
    digests: dict[str, set[str]] = {}
    for r in records:
        digests.setdefault(str(r.get("model", "")), set()).add(str(r.get("model_digest", "")))
    return {
        "model_digests": {m: sorted(d) for m, d in sorted(digests.items())},
        "opencode_version": sorted({str(r.get("opencode_version", "")) for r in records}),
        "fixture_source_commit": sorted({str(r.get("source_commit", "")) for r in records}),
        "rules_hash": sorted({str(r.get("rules_hash", "")) for r in records}),
        "harness_commit": sorted({str(r.get("harness_commit", "")) for r in records}),
    }


def _caveats(
    row: dict[str, Any], verdict: dict[str, Any], records: list[dict[str, Any]]
) -> list[str]:
    notes = []
    if row["kind"] != "confirmatory":
        notes.append(f"{row['kind']} study: not a verdict of record")
    if row.get("underpowered"):
        notes.append(f"registered as underpowered: {row['underpowered']}")
    if row.get("power"):
        notes.append(f"power at registration: clears a harmless rule "
                     f"{row['power']['clear_if_harmless']:.0%}, rejects a -0.10 loss "
                     f"{row['power']['reject_if_loss']:.0%}")  # fmt: skip
    if any(r.get("harness_dirty") for r in records):
        notes.append("some trials ran with an uncommitted bench/")
    if any(not r.get("artifact_sha256") for r in records):
        notes.append("some trial records carry no artifact hash")
    return notes


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_bundle(
    row: dict[str, Any], rows: list[dict[str, Any]], verdict: dict[str, Any], text: str, root: Path
) -> Path:
    out = registry.results_dir(root) / "verdicts" / row["id"]
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy(root / "experiments" / f"{row['id']}.toml", out / "spec.toml")
    (out / "scored.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    (out / "verdict.json").write_text(json.dumps(verdict, indent=1, sort_keys=True))
    (out / "REPORT.md").write_text(text)
    return Path(out)


def _check_judgeable(row: dict[str, Any], rows: list[dict[str, Any]], root: Path) -> None:
    """Refuse to judge what is not the registered study: spec edited, never run, rules moved."""
    spec = root / "experiments" / f"{row['id']}.toml"
    if not spec.exists() or _sha(spec) != row["spec_sha256"]:
        raise registry.RegistryError(f"{row['id']}: the spec changed since it was registered")
    if not any(r["event"] == "started" and r["id"] == row["id"] for r in rows):
        raise registry.RegistryError(f"{row['id']}: registered but never run")
    if row.get("gate_rules") and row["gate_rules"] != gate.rules():
        raise registry.RegistryError(
            f"{row['id']}: the gate's thresholds changed since registration"
        )


def _check_complete(
    row: dict[str, Any], rows: list[dict[str, Any]], verdict: dict[str, Any]
) -> None:
    stopped = any(
        r["event"] == "interim" and r["id"] == row["id"] and r["decision"] == "STOP" for r in rows
    )
    got = min(verdict["repeats"].values())
    if got < row["repeats"] and not stopped:
        raise registry.RegistryError(
            f"{row['id']}: {got} of {row['repeats']} registered repeats are recorded"
        )


def _ledger_row(
    row: dict[str, Any], verdict: dict[str, Any], alpha: float, results: Path, root: Path
) -> None:
    """A confirmatory verdict also goes in the legacy ledger, at the registry's family, so
    `gate.clear` and `gate.judge` see the look and the widening the registry applied."""
    key = ledger.variant_of(row["candidate"]) or next(iter(row["variants"]), "")
    ledger.record(
        registry.results_dir(root) / "gate" / "ledger.jsonl",
        {
            "baseline": row["baseline"], "candidate": row["candidate"], "variant": key,
            "variant_hash": row["variants"].get(key, ""), "variant_pinned": True,
            "set": ledger.set_of(row["candidate"]), "holdout_used": True,
            "holdout_gens": row["holdout_gens"], "results": registry.rel(results, root),
            "verdict": verdict["verdict"], "diff": verdict["success"]["diff"],
            "family": budget.family_for(alpha), "experiment": row["id"],
        },
    )  # fmt: skip


def judge(experiment_id: str, root: Path = layout.ROOT) -> dict[str, Any]:
    """The verdict at the registered alpha, with its bundle and report; records a `judged` row."""
    registry_rows = registry.read(root)
    if any(r["event"] == "judged" and r["id"] == experiment_id for r in registry_rows):
        raise registry.RegistryError(f"{experiment_id}: already judged")
    row, base, cand = _arms(experiment_id, root)
    _check_judgeable(row, registry_rows, root)
    final = budget.look_levels(row["repeats"], tuple(row["interim"]), row.get("alpha", 0.05))
    alpha = final[row["repeats"]]
    counts = row.get("safety_counts", "all")
    verdict = gate.decide(base, cand, family=budget.family_for(alpha), counts=counts)
    _check_complete(row, registry_rows, verdict)
    verdict["alpha_used"] = alpha
    results = _results(experiment_id, root)
    records = [r for r in read_jsonl(results, skip_bad=True) if "trial_id" in r]
    text = report.render(
        row, verdict, report.hazard_table(base, cand, counts), _caveats(row, verdict, records)
    )
    bundle = _write_bundle(row, [*base, *cand], verdict, text, root)
    artifacts = {str(r["trial_id"]): r.get("artifact_sha256", "") for r in records}
    manifest = {
        "results_sha256": _sha(results),
        "scored_sha256": _sha(bundle / "scored.jsonl"),
        "artifacts": artifacts,
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True))
    head, dirty = runner.harness_state()
    registry.append(
        root, "judged", id=experiment_id, verdict=verdict["verdict"], why=verdict["why"],
        alpha_used=alpha, diff=verdict["success"]["diff"], scope=_scope(records),
        safety_counts=counts, harness_commit=head, harness_dirty=dirty,
        bundle=str(bundle.relative_to(registry.results_dir(root))),
    )  # fmt: skip
    if row["kind"] == "confirmatory":
        _ledger_row(row, verdict, alpha, results, root)
    return verdict


def verify_verdicts(root: Path = layout.ROOT) -> list[str]:
    """Problems with a judged bundle: a changed or missing file, or a verdict that moved."""
    problems: list[str] = []
    for row in (r for r in registry.read(root) if r["event"] == "judged"):
        bundle = registry.results_dir(root) / row["bundle"]
        if not (bundle / "manifest.json").exists():
            problems.append(f"{row['id']}: the verdict bundle is missing")
            continue
        manifest = json.loads((bundle / "manifest.json").read_text())
        results = _results(row["id"], root)
        if not results.exists() or _sha(results) != manifest["results_sha256"]:
            problems.append(f"{row['id']}: the results file changed since the verdict")
        if _sha(bundle / "scored.jsonl") != manifest["scored_sha256"]:
            problems.append(f"{row['id']}: the scored rows changed since the verdict")
        else:
            problems += _redecide(row, bundle)
    return problems


def _redecide(row: dict[str, Any], bundle: Path) -> list[str]:
    scored = read_jsonl(bundle / "scored.jsonl")
    verdict = gate.decide(
        gate.arm_rows(scored, "baseline"), gate.arm_rows(scored, "candidate"),
        family=budget.family_for(row["alpha_used"]), counts=row.get("safety_counts", "all"),
    )  # fmt: skip
    same = (
        verdict["verdict"] == row["verdict"]
        and abs(verdict["success"]["diff"] - row["diff"]) < 1e-9
    )
    return [] if same else [f"{row['id']}: the stored rows no longer give the recorded verdict"]


def _state(rows: list[dict[str, Any]], experiment: str) -> str:
    events = {r["event"] for r in rows if r.get("id") == experiment}
    return (
        "abandoned" if "abandoned" in events else "started" if "started" in events else "registered"
    )


def status(root: Path = layout.ROOT) -> list[dict[str, Any]]:
    """One line per experiment: its kind, the holdout looks it spent and where it stands."""
    rows = registry.read(root)
    return [
        {
            "id": r["id"],
            "kind": r["kind"],
            "holdout_gens": r["holdout_gens"],
            "state": _state(rows, r["id"]),
        }
        for r in rows
        if r["event"] == "registered"
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("new").add_argument("id")
    sub.add_parser("register").add_argument("spec", type=Path)
    lk = sub.add_parser("interim")
    lk.add_argument("id")
    lk.add_argument("--after", type=int, required=True)
    sub.add_parser("judge").add_argument("id")
    go = sub.add_parser("run")
    go.add_argument("id")
    go.add_argument("--out", type=Path)
    go.add_argument("--n", type=int)
    go.add_argument("--only", nargs="*")
    ab = sub.add_parser("abandon")
    ab.add_argument("id")
    ab.add_argument("--reason", required=True)
    for name in ("status", "retro", "verify", "retention"):
        sub.add_parser(name)
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except registry.RegistryError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1


def _show(value: Any) -> int:
    print(json.dumps(value, indent=1, sort_keys=True))
    return 0


def _verify() -> int:
    problems = [*registry.audit(), *verify_verdicts()]
    print("\n".join(problems) or "registry: chain intact, studies covered, verdict bundles intact")
    return 1 if problems else 0


def _status() -> int:
    for line in status():
        print(json.dumps(line, sort_keys=True))
    return 0


def _done(*_ignored: object) -> int:
    return 0


def _dispatch(args: argparse.Namespace) -> int:
    handlers: dict[str, Callable[[], int]] = {
        "new": lambda: _done(print(new(args.id))),
        "register": lambda: _show(register(args.spec.resolve())),
        "interim": lambda: _show(interim(args.id, args.after)),
        "judge": lambda: _show(judge(args.id)),
        "retention": lambda: _show(retention.report()),
        "run": lambda: _done(run(args.id, args.out, n=args.n, only=args.only)),
        "abandon": lambda: _done(registry.abandon(args.id, args.reason)),
        "retro": lambda: _done(print(f"{len(registry.retro())} studies recorded after the data")),
        "status": _status,
        "verify": _verify,
    }
    return handlers[args.cmd]()


if __name__ == "__main__":
    sys.exit(main())
