"""The built fixture keeps the kept strategies registered and the private strategy's stub working.

Phase 2 acceptance. The stub's module, class and function names are read from the fixture's own
stubs (`fixtures/paramo/stubs/`), so this file names none of the private code; without a built
fixture (a clone of the harness alone) the test skips and says why.
"""

from __future__ import annotations

import ast
from pathlib import Path
from string import Template

import pytest

from bench.issues import check
from tests.helpers import bwrap_works as _bwrap_works

ROOT = Path(__file__).resolve().parents[1]
FX = ROOT / "fixtures" / "paramo"
ENV = check.Env(FX / "versions/v2/tree", FX / "venv/v2", FX / "data/v3/root")
STUBS = FX / "stubs"

SCRIPT = Template("""python - <<'PY'
import logging
from engine.screener import base
print("REGISTERED", sorted(base._source_registry()))
import config.trading.strategies as s
print("MIXINS", [m.__name__ for m in s.STRATEGY_MIXINS])
from engine.screener.$source import $signal
from config.trading.strategies.$config import $fields, $validate
stub = $signal(object(), logging.getLogger("x"))
print("STUB_EMPTY", stub.scan_day(None, None, {}, {}) == [])
print("STUB_VALID", $validate($fields()) == [])
from engine.screener import insider_cluster_source as ic, fundamentals_composite_source as fc
print("KEPT_IMPORT", ic.InsiderClusterSignalSource.__name__,
      fc.FundamentalsCompositeSignalSource.__name__)
PY""")


def _defined(path: Path, suffix: str = "", prefix: str = "") -> list[str]:
    tree = ast.parse(path.read_text())
    return [
        n.name
        for n in tree.body
        if isinstance(n, ast.ClassDef | ast.FunctionDef)
        and n.name.endswith(suffix)
        and n.name.startswith(prefix)
    ]


def stub_names(stubs: Path = STUBS) -> dict[str, str] | None:
    """The stubbed strategy's config module, source module, mixin, validator and source class."""
    config = sorted((stubs / "config/trading/strategies").glob("*.py"))
    source = sorted((stubs / "engine/screener").glob("*_source.py"))
    if len(config) != 1 or len(source) != 1:
        return None
    found = {
        "fields": _defined(config[0], suffix="Fields"),
        "validate": _defined(config[0], prefix="validate_"),
        "signal": _defined(source[0], suffix="SignalSource"),
    }
    if any(len(v) != 1 for v in found.values()):
        return None
    modules = {"config": config[0].stem, "source": source[0].stem}
    return modules | {k: v[0] for k, v in found.items()}


NAMES = stub_names()


@pytest.mark.skipif(
    not _bwrap_works() or not ENV.venv.exists() or not ENV.tree.exists(),
    reason="needs bwrap and the built fixture",
)
@pytest.mark.skipif(NAMES is None, reason="the fixture's one strategy stub is not present")
def test_kept_strategies_register_and_the_stub_is_inert_but_valid() -> None:
    assert NAMES is not None
    out = check.run_cmd(ENV, SCRIPT.substitute(NAMES))
    assert out.passed, out.tail
    text = out.tail
    # as in the real repo: insider_cluster is the sole strategy, loadtest a load generator
    assert "REGISTERED ['insider_cluster', 'loadtest']" in text
    for mixin in (
        "SharedStrategyFields",
        NAMES["fields"],
        "InsiderClusterFields",
        "FundamentalsCompositeFields",
        "LoadtestFields",
    ):
        assert mixin in text.split("MIXINS")[1].split("\n")[0]
    assert "STUB_EMPTY True" in text and "STUB_VALID True" in text
    assert (
        "KEPT_IMPORT InsiderClusterSignalSource FundamentalsCompositeSignalSource"
        in text.replace("\n", " ")
    )


def test_stub_names_are_read_from_the_stub_files(tmp_path: Path) -> None:
    (tmp_path / "config/trading/strategies").mkdir(parents=True)
    (tmp_path / "engine/screener").mkdir(parents=True)
    assert stub_names(tmp_path) is None, "no stub: nothing to check"
    (tmp_path / "config/trading/strategies/secret_strategy.py").write_text(
        "class SecretFields: ...\ndef validate_secret(s): return []\ndef helper(): ...\n"
    )
    (tmp_path / "engine/screener/secret_source.py").write_text("class SecretSignalSource: ...\n")
    assert stub_names(tmp_path) == {
        "config": "secret_strategy",
        "source": "secret_source",
        "fields": "SecretFields",
        "validate": "validate_secret",
        "signal": "SecretSignalSource",
    }
