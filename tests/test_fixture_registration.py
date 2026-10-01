"""The built fixture keeps the three kept strategies and the stub working (Phase 2 acceptance)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bench.issues import check
from tests.helpers import bwrap_works as _bwrap_works

ROOT = Path(__file__).resolve().parents[1]
FX = ROOT / "fixtures" / "paramo"
ENV = check.Env(FX / "versions/v2/tree", FX / "venv/v2", FX / "data/v3/root")

SCRIPT = """python - <<'PY'
import logging
from engine.screener import base
print("REGISTERED", sorted(base._source_registry()))
import config.trading.strategies as s
print("MIXINS", [m.__name__ for m in s.STRATEGY_MIXINS])
from engine.screener.private_strategy_source import PrivateStrategySignalSource
from config.trading.strategies.private_strategy import PrivateStrategyFields, validate_private_strategy
stub = PrivateStrategySignalSource(object(), logging.getLogger("x"))
print("STUB_EMPTY", stub.scan_day(None, None, {}, {}) == [])
print("STUB_VALID", validate_private_strategy(PrivateStrategyFields()) == [])
from engine.screener import insider_cluster_source as ic, fundamentals_composite_source as fc
print("KEPT_IMPORT", ic.InsiderClusterSignalSource.__name__,
      fc.FundamentalsCompositeSignalSource.__name__)
PY"""


@pytest.mark.skipif(
    not _bwrap_works() or not ENV.venv.exists() or not ENV.tree.exists(),
    reason="needs bwrap and the built fixture",
)
def test_kept_strategies_register_and_the_stub_is_inert_but_valid() -> None:
    out = check.run_cmd(ENV, SCRIPT)
    assert out.passed, out.tail
    text = out.tail
    # as in the real repo: insider_cluster is the sole strategy, loadtest a load generator
    assert "REGISTERED ['insider_cluster', 'loadtest']" in text
    for mixin in (
        "SharedStrategyFields",
        "PrivateStrategyFields",
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
