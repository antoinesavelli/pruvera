"""Spike: where does the fixture's extra startup time go? Per-request traces of both sides.

Run from the repo root: `PYTHONPATH=. python3 spikes/latency_trace.py run --n 6` then `... report`.
Each trial's Ollama filter records, per request, first-byte latency and hashes of the system
message, the tools and the conversation so far (never text). If the fixture is slower because its
requests share no prefix with each other (Ollama cannot reuse its prompt cache), its system hash
varies between trials while the reference's does not; if first-byte latency differs with equal
hashes, the cause is elsewhere. Depends on: bench.{layout,reference,runner,preflight}; Ollama.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from pathlib import Path

from bench import layout, preflight, reference, runner

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results" / "realism" / "latency-trace-1.jsonl"
PROMPT = (
    "According to docs/STATUS.md, which strategy is the operative default SOURCE? "
    "Reply with the strategy name only."
)


def run(n: int, seed: int) -> None:
    fixture = runner.load_profile(layout.VERSION, "clean")
    artifacts, trials = ROOT / "artifacts", ROOT / "overlays"
    order = ["fixture", "reference"] * n
    random.Random(seed).shuffle(order)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with preflight.session_lock(), reference.cleanup_on_signals():
        reference.sweep(trials)
        source = reference.prepare(layout.REAL_REPO, layout.source_commit(), trials / "ref-source")
        try:
            for i, side in enumerate(order):
                preflight.wait_clear(120)
                spec = runner.TrialSpec(
                    agent="research", model="gpt-oss:20b-64k", prompt=PROMPT, label="latency-trace"
                )
                if side == "fixture":
                    runner.run_trial(fixture, spec, artifacts, trials, OUT)
                else:
                    rev = layout.source_commit()
                    reference.run_reference(spec, source, artifacts, trials, OUT, rev=rev)
                print(i + 1, side, flush=True)
        finally:
            reference.discard(trials / "ref-source")


def report() -> None:
    rows = [json.loads(line) for line in OUT.read_text().splitlines() if line.strip()]
    by_side: dict[str, list[dict]] = {"fixture": [], "reference": []}
    for r in rows:
        trace = Path(r["artifact"]) / "ollama_trace.jsonl"
        calls = [json.loads(x) for x in trace.read_text().splitlines()] if trace.exists() else []
        by_side[r["environment"]].append({"secs": r["secs"], "phases": r["phases"], "calls": calls})
    for side, trials in by_side.items():
        first = [t["calls"][0] for t in trials if t["calls"]]
        print(f"\n{side}: {len(trials)} trials")
        print("  secs", [t["secs"] for t in trials])
        print("  first-call first-byte s", [c["first_byte_s"] for c in first])
        print("  system hashes", sorted({c.get("system") for c in first}))
        print("  tools hashes", sorted({c.get("tools") for c in first}))
        print("  first-call body_len", sorted({c.get("body_len") for c in first}))
        later = [c["first_byte_s"] for t in trials for c in t["calls"][1:]]
        if later:
            print("  later-call first-byte median", round(statistics.median(later), 2))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    go = sub.add_parser("run")
    go.add_argument("--n", type=int, default=6)
    go.add_argument("--seed", type=int, default=1)
    sub.add_parser("report")
    args = ap.parse_args()
    run(args.n, args.seed) if args.cmd == "run" else report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
