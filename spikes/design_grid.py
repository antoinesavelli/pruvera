"""How big must generation 3 be? Simulate the gate over designs (CPU only, no model runs).

For each design (issues, safety issues, repeats, safety repeats, unsafe rate) it reports how often
the gate CLEARs a harmless rule, CLEARs a +0.10 gain, and REJECTs a true -0.10 loss, at the alpha
share of the first study in a fresh generation (0.025). Issues are bimodal (solved about 10% or
90% of the time), as the real campaigns look. Output: `results/gate/design-grid-<date>.txt`.
Depends on: bench.{budget,gate}.
"""

from __future__ import annotations

import itertools
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench import budget, gate  # noqa: E402

ISSUES = (13, 25, 35, 50)
SAFETY = (4, 8, 12, 16)
SAFETY_REPEATS = (6, 12)
UNSAFE = (0.75, 0.5)
REPS, DRAWS = 150, 400


def cell(n: int, s: int, repeats: int, safety_repeats: int, unsafe: float) -> dict[str, float]:
    design = gate.Design(
        (True,) * s + (False,) * (n - s), (safety_repeats,) * s + (repeats,) * (n - s)
    )
    kw = {
        "design": design, "family": budget.family_for(budget.share(0)), "reps": REPS,
        "draws": DRAWS, "bimodal": True, "unsafe_rate": unsafe, "cand_unsafe_rate": unsafe,
    }  # fmt: skip
    return {
        "clear_null": gate.calibrate(0.0, **kw)["CLEAR"] / REPS,
        "clear_plus10": gate.calibrate(0.10, **kw)["CLEAR"] / REPS,
        "reject_minus10": gate.calibrate(-0.10, **kw)["REJECT"] / REPS,
    }


def main() -> None:
    stamp = time.strftime("%Y-%m-%d")
    out = Path(__file__).resolve().parents[1] / "results" / "gate" / f"design-grid-{stamp}.txt"
    lines = [
        f"# alpha share {budget.share(0)}, repeats 6 per non-safety issue, {REPS} gates per cell, "
        f"{DRAWS} bootstrap draws, bimodal issues; command: python3 spikes/design_grid.py",
        "issues safety safety_repeats unsafe | clear_null clear_+0.10 reject_-0.10 | trials/arm",
    ]
    for n, s, sr, u in itertools.product(ISSUES, SAFETY, SAFETY_REPEATS, UNSAFE):
        if s * 2 > n:
            continue
        r = cell(n, s, 6, sr, u)
        trials = s * sr + (n - s) * 6
        lines.append(
            f"{n:>6} {s:>6} {sr:>13} {u:>6} | {r['clear_null']:>10.2f} {r['clear_plus10']:>11.2f} "
            f"{r['reject_minus10']:>13.2f} | {trials}"
        )
        print(lines[-1], flush=True)
    out.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
