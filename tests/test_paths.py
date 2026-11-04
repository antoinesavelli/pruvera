"""The per-machine paths file: strict loading, generic defaults when absent, and a consumer."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from bench import compare, layout, paths, reference, sandbox

ROOT = Path(__file__).resolve().parents[1]
GOOD: dict[str, Any] = {
    "repo": "/x/Paramo",
    "data_root": "/x/trading",
    "archive_root": "/x/archive",
    "opencode_global": "/x/oc/opencode.json",
    "reference_overlays": ["/a/o1", "/b/o2"],
}


def _write(tmp_path: Path, data: object) -> Path:
    """Writes `data` as the paths file in a temp dir and returns the file."""
    file = tmp_path / "paths.json"
    file.write_text(data if isinstance(data, str) else json.dumps(data))
    return file


def _home(tmp_path: Path, name: str, data: object = None) -> Path:
    """A HOME in tmp_path, holding the paths file with `data` when it is given."""
    home = tmp_path / name
    (home / ".config" / "pruvera").mkdir(parents=True)
    if data is not None:
        (home / ".config" / "pruvera" / "paths.json").write_text(json.dumps(data))
    return home


def _state(tmp_path: Path, name: str, built: bool) -> Path:
    """A state folder, with a built fixture version in it when `built`."""
    state = tmp_path / name
    (state / "fixtures" / "paramo" / "versions" / "v1" / ("tree" if built else "other")).mkdir(
        parents=True, exist_ok=True
    )
    return state


def _py(home: Path, state: Path, code: str) -> subprocess.CompletedProcess[str]:
    """Runs `code` in a fresh interpreter with this HOME and AGENT_TESTING_STATE."""
    env = {**os.environ, "HOME": str(home), "AGENT_TESTING_STATE": str(state)}
    return subprocess.run(
        [sys.executable, "-B", "-c", code], cwd=ROOT, env=env, capture_output=True, text=True
    )


def _run(home: Path, state: Path, code: str) -> str:
    """Stdout of `code` run by `_py`, which must succeed."""
    out = _py(home, state, code)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_load_returns_what_the_file_says(tmp_path: Path) -> None:
    """A complete file loads to a Paths with the same values, the overlays as a tuple."""
    assert paths.load(_write(tmp_path, GOOD)) == paths.Paths(
        "/x/Paramo", "/x/trading", "/x/archive", "/x/oc/opencode.json", ("/a/o1", "/b/o2")
    )


@pytest.mark.parametrize(
    "data",
    [
        "{not json",
        "[]",
        '"/x/Paramo"',
        {**GOOD, "extra": "/x/y"},
        {k: v for k, v in GOOD.items() if k != "archive_root"},
        {k: v for k, v in GOOD.items() if k != "reference_overlays"},
        {},
        {**GOOD, "repo": 5},
        {**GOOD, "repo": True},
        {**GOOD, "repo": None},
        {**GOOD, "repo": ""},
        {**GOOD, "repo": ["/x/Paramo"]},
        {**GOOD, "reference_overlays": "/a/o1"},
        {**GOOD, "reference_overlays": []},
        {**GOOD, "reference_overlays": ["/a/o1", 3]},
        {**GOOD, "reference_overlays": ["/a/o1", "rel/o2"]},
        {**GOOD, "data_root": "x/trading"},
        {**GOOD, "data_root": "/x/trading/"},
        {**GOOD, "data_root": "/x//trading"},
        {**GOOD, "data_root": "/x/./trading"},
        {**GOOD, "data_root": "/x/../trading"},
        {**GOOD, "data_root": "//x/trading"},
        {**GOOD, "data_root": "/"},
        {**GOOD, "reference_overlays": ["//a/o1"]},
        {**GOOD, "reference_overlays": ["/a/o1/"]},
    ],
    ids=lambda d: json.dumps(d)[:60],
)
def test_load_refuses_a_bad_file(tmp_path: Path, data: object) -> None:
    """Invalid JSON, a wrong top level, an unknown or missing key, a wrong type or a path that is
    relative, not normal or has `..` raises ValueError: nothing is silently defaulted."""
    with pytest.raises(ValueError):
        paths.load(_write(tmp_path, data))


def test_the_defaults_are_generic_and_pass_the_loader(tmp_path: Path) -> None:
    """The defaults sit under the generic root and are accepted by the loader's own rules."""
    as_json = {
        **paths.DEFAULTS.__dict__,
        "reference_overlays": list(paths.DEFAULTS.reference_overlays),
    }
    assert paths.load(_write(tmp_path, as_json)) == paths.DEFAULTS
    assert all(
        p.startswith("/srv/pruvera/")
        for p in (
            *as_json["reference_overlays"],
            *(v for v in as_json.values() if isinstance(v, str)),
        )
    )


def test_the_modules_that_name_the_real_repo_take_it_from_the_paths() -> None:
    """layout, the sandbox, attempts and the reference venv all follow PATHS.repo."""
    assert layout.REAL_REPO == Path(paths.PATHS.repo)
    assert sandbox.WORKDIR == paths.PATHS.repo
    assert reference.REAL_VENV == layout.REAL_REPO / ".venv"


def test_defaults_only_when_there_is_no_file(tmp_path: Path) -> None:
    """With no file CONFIGURED is False and PATHS the defaults; with one, its values are used."""
    code = (
        "import json; from bench import paths; "
        "print(json.dumps([paths.CONFIGURED, paths.PATHS == paths.DEFAULTS, paths.PATHS.repo]))"
    )
    state = _state(tmp_path, "state", built=False)
    assert json.loads(_run(_home(tmp_path, "empty"), state, code)) == [
        False,
        True,
        paths.DEFAULTS.repo,
    ]
    assert json.loads(_run(_home(tmp_path, "home", GOOD), state, code)) == [
        True,
        False,
        "/x/Paramo",
    ]


def test_a_bad_file_fails_at_import(tmp_path: Path) -> None:
    """A partial file makes `import bench.paths` fail; it never mixes in the defaults."""
    home = _home(tmp_path, "home", {"repo": "/x/Paramo"})
    out = _py(home, _state(tmp_path, "state", built=False), "import bench.paths")
    assert out.returncode != 0 and "missing keys" in out.stderr


def test_compare_strips_the_configured_repo_and_every_configured_overlay(tmp_path: Path) -> None:
    """compare's prefix regex is built from the file's values: the repo and each overlay root,
    with dots taken literally and a sibling directory left alone."""
    home = _home(
        tmp_path, "home", {**GOOD, "repo": "/x/P.aramo", "reference_overlays": ["/a/o.1", "/b/o2"]}
    )
    code = (
        "import json; from bench import compare; "
        "print(json.dumps([compare.normalise(t) for t in ["
        "'/x/P.aramo/a.py', '/a/o.1/ref-0a1b/work/b.py', '/b/o2/ref-ff/work/c.py', "
        "'/x/PXaramo/a.py', '/a/oX1/ref-0a1b/work/b.py', '/a/o.1/ref-zz/work/b.py']]))"
    )
    assert json.loads(_run(home, _state(tmp_path, "state", built=False), code)) == [
        "a.py",
        "b.py",
        "c.py",
        "/x/PXaramo/a.py",
        "/a/oX1/ref-0a1b/work/b.py",
        "/a/o.1/ref-zz/work/b.py",
    ]


def test_compare_strips_this_machines_paths() -> None:
    """With whatever file this machine has, the repo and every overlay root normalise away."""
    assert compare.normalise(f"{paths.PATHS.repo}/pkg/m.py") == "pkg/m.py"
    for overlay in paths.PATHS.reference_overlays:
        assert compare.normalise(f"{overlay}/ref-0a1b/work/pkg/m.py") == "pkg/m.py"


MODULES = [
    "bench.paths",
    "bench.compare",
    "bench.sandbox",
    "bench.layout",
    "bench.issues.attempts",
    "bench.research.grade",
    "bench.research.open_grade",
    "bench.realism",
    "bench.runner",
]


@pytest.mark.parametrize("module", MODULES)
def test_a_bench_process_cannot_start_on_a_built_fixture_without_the_file(
    tmp_path: Path, module: str
) -> None:
    """Importing any module that names a real path fails with the message when a fixture is
    built and there is no file; with the file, or with no built fixture, it imports and the
    no-file case gives the generic defaults. It reads the state folder only (DECISIONS row 12)."""
    code = f"import {module}"
    built = _py(_home(tmp_path, "none"), _state(tmp_path, "built", built=True), code)
    assert built.returncode != 0 and "PathsFileError" in built.stderr
    assert "paths.json" in built.stderr and "generic defaults" in built.stderr
    with_file = _py(_home(tmp_path, "file", GOOD), _state(tmp_path, "built", built=True), code)
    assert with_file.returncode == 0, with_file.stderr
    bare = _py(_home(tmp_path, "bare"), _state(tmp_path, "bare", built=False), code)
    assert bare.returncode == 0, bare.stderr


def test_without_a_fixture_or_a_file_the_modules_get_the_defaults(tmp_path: Path) -> None:
    """On a machine with neither (CI), the repo path every module uses is the generic default."""
    code = (
        "import bench.sandbox, bench.layout, bench.paths; "
        "print(bench.sandbox.WORKDIR, bench.layout.REAL_REPO, "
        "bench.paths.PATHS == bench.paths.DEFAULTS)"
    )
    out = _run(_home(tmp_path, "bare"), _state(tmp_path, "bare", built=False), code)
    default = paths.DEFAULTS.repo
    assert out.split() == [default, default, "True"]
