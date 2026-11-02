"""No new gitignored script: a script that is gitignored has no history and no review.

The tracked `.gitignore` lists nine `.py` files that exist on disk only. This test freezes that
set. A new entry fails it: put the script in `bench/` or `spikes/` with a test, or keep it in the
session scratchpad. Removing an entry fails it too, so the set is shortened on purpose, in the
same change, when one of these scripts is retired.
Depends on: pytest.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FROZEN = {
    "/bench/issues/hand_kinds.py",
    "/bench/issues/injections.py",
    "/tests/test_legacy_bench.py",
    "/variants/g2-audit.py",
    "/variants/g2-pilot.py",
    "/variants/g2-power.py",
    "/variants/g3-power.py",
    "/variants/gitfix-power.py",
    "/variants/gitfix_study.py",
}


def test_gitignored_scripts_are_the_frozen_set() -> None:
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    found = {
        line.strip()
        for line in lines
        if line.strip().endswith(".py") and not line.strip().startswith("#")
    }
    assert found == FROZEN, (
        "a new gitignored script has no history; put it in bench/ or spikes/ with a test, "
        f"or in the session scratchpad. New: {sorted(found - FROZEN)}; "
        f"removed (shorten FROZEN on purpose): {sorted(FROZEN - found)}"
    )
