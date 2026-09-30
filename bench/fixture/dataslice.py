"""Build the data root a trial sees at the real default path: the golden smoke slice, by name.

The slice is real (five symbols, one backtest window); everything the code reads beyond it is absent
or synthetic. Depends on: bench.sandbox (constants are resolved by importing the fixture's own
config.paths inside the sandbox), an interpreter with the fixture's dependencies bound at /venv.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from bench import sandbox

SLICE_MAP = {  # golden smoke slice directory -> config/paths.py constant it stands in for
    "ticker_data": "TICKERS_DIR",
    "daily_aggregates": "DAILY_AGGREGATES_DIR",
    "market_context": "MARKET_CONTEXT_DIR",
    "halts": "HALTS_DIR",
    "insider_txn": "INSIDER_TXN_DIR",
    "form4_footnotes": "FORM4_FOOTNOTES_DIR",
}
_RESOLVE = """
import json, sqlite3, sys, pathlib
sys.path.insert(0, "/work")
from config import paths
names = {names}
out = {{n: getattr(paths, n) for n in names + ["DATA_ROOT", "SYSTEM_DB_PATH"]}}
if {init_db}:
    from db import migrations
    pathlib.Path(paths.SYSTEM_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(paths.SYSTEM_DB_PATH)
    migrations.initialize(conn)
    conn.close()
print(json.dumps(out))
"""


class DataSliceError(RuntimeError):
    """The slice could not be placed."""


def build(
    tree: Path, out_root: Path, workdir: Path, venv: Path, init_db: bool = True
) -> dict[str, str]:
    """Create `out_root` (the data root) from the tree's golden smoke slice; return the paths."""
    slice_dir = tree / "tests" / "golden" / "smoke" / "data"
    if not slice_dir.is_dir():
        raise DataSliceError(f"no golden smoke slice at {slice_dir}")
    if out_root.exists():
        raise DataSliceError(f"data root already exists: {out_root}")
    out_root.mkdir(parents=True)
    for sub in ("upper", "work"):
        (workdir / sub).mkdir(parents=True, exist_ok=True)
    for sub in ("config", "data", "state"):
        (workdir / "xdg" / sub).mkdir(parents=True, exist_ok=True)
    spec = sandbox.Spec(
        base=tree,
        upper=workdir / "upper",
        work=workdir / "work",
        xdg=workdir / "xdg",
        net="none",
        ro_binds=((venv, "/venv"),),
        rw_binds=((out_root, "/dataroot"),),
        env={"PARAMO_DATA_ROOT": "/dataroot", "PATH": "/venv/bin:/usr/bin:/bin"},
    )
    code = _RESOLVE.format(names=list(SLICE_MAP.values()), init_db=init_db)
    done = sandbox.run(spec, ["/venv/bin/python", "-c", code], timeout=300)
    if done.returncode != 0:
        raise DataSliceError(f"resolving paths failed: {done.stderr[-400:]}")
    resolved: dict[str, str] = json.loads(done.stdout.strip().splitlines()[-1])
    for src_name, const in SLICE_MAP.items():
        src = slice_dir / src_name
        if not src.is_dir():
            continue
        dest = out_root / Path(resolved[const]).relative_to("/dataroot")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest)
    sandbox.remove_trial_dirs(workdir / "upper", workdir / "work")
    return resolved
