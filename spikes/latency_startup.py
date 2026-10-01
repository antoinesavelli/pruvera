"""Spike: time opencode's first event in the sandbox on the fixture vs a real-repo copy.

Run from the repo root: `PYTHONPATH=. python3 spikes/latency_startup.py`. Each case runs 4 times,
order flipped per repeat. 2026-09-30: a trivial prompt takes about 9 s to the first event on both
sides; a prompt that reads the repo (the STATUS question) takes 13.5 s on the fixture, 9 s on the
real copy. That is the unexplained startup gap in `PLAN.md` Phase 5; this script varies the mounts.
Depends on: bench.{sandbox,agentconfig,runner,layout,reference,preflight,transcript}; Ollama.
"""

from __future__ import annotations

import json
import statistics
import tempfile
import time
from pathlib import Path

from bench import agentconfig, layout, preflight, reference, runner, sandbox
from bench.transcript import Transcript

PROMPT = (
    "According to docs/STATUS.md, which strategy is the operative default SOURCE? "
    "Reply with the strategy name only."
)


def run_once(base: Path, venv: Path, data: Path | None) -> tuple[float, float]:
    preflight.wait_clear(60)
    tmp = Path(tempfile.mkdtemp(prefix="lat-"))
    for d in ("upper", "work", "xdg/config", "xdg/data", "xdg/state"):
        (tmp / d).mkdir(parents=True)
    asm = agentconfig.assemble("research", "gpt-oss:20b-64k")
    agentconfig.write(asm, tmp / "xdg" / "config")
    binds = [
        (runner.OPENCODE.resolve(), "/opt/bin/opencode"),
        (runner.RIPGREP.resolve(), f"{sandbox.HOME}/.cache/opencode/bin/rg"),
        (venv, sandbox.VENV_DIR),
    ]
    env = {
        "PATH": f"/opt/bin:{sandbox.VENV_DIR}/bin:/usr/bin:/bin",
        **runner.GIT_IDENTITY,
        "OPENCODE_CONFIG_CONTENT": json.dumps(asm.inline),
    }
    probe = runner.TrialSpec(agent="research", model="gpt-oss:20b-64k", prompt="x")
    spec = sandbox.Spec(
        base=base,
        upper=tmp / "upper",
        work=tmp / "work",
        xdg=tmp / "xdg",
        ro_binds=tuple(binds),
        env=env,
        net="ollama",
        data_base=data,
        ollama_models=runner._allowed_models(asm, probe),
    )
    started = time.time()
    argv = ["opencode", "run", "--agent", "research", "--format", "json", PROMPT]
    with sandbox.popen(spec, argv) as proc:
        out, _ = proc.communicate(timeout=120)
    tr = Transcript().parse(out)
    assert tr.first_ts is not None
    sandbox.remove_trial_dirs(tmp)
    return tr.first_ts / 1000 - started, time.time() - started


def main() -> None:
    src = Path(tempfile.mkdtemp(prefix="ref-lat-"))
    reference.prepare(layout.REAL_REPO, layout.source_commit(), src)
    cases = {
        "fixture": (layout.tree(), layout.venv(), layout.data_root()),
        "real": (src / "tree", reference.REAL_VENV, None),
    }
    results: dict[str, list[tuple[float, float]]] = {name: [] for name in cases}
    for rep in range(4):
        for name in list(cases) if rep % 2 == 0 else list(cases)[::-1]:
            results[name].append(run_once(*cases[name]))
    for name, rows in results.items():
        print(f"{name:8s} first event median {statistics.median(r[0] for r in rows):.1f}s")
    sandbox.remove_trial_dirs(src)


if __name__ == "__main__":
    main()
