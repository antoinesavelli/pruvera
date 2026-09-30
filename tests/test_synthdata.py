"""Tests for the synthetic aggregates generator (needs pandas: fixture venv python)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pd = pytest.importorskip("pandas")

from bench.fixture import synthdata  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = ROOT / "fixtures/paramo/venv/v2/bin/python"


def _template() -> Any:
    return pd.DataFrame(
        {
            "date": pd.Series([], dtype="datetime64[ns]"),
            "symbol": pd.Series([], dtype="str"),
            "open": pd.Series([], dtype="float32"),
            "high": pd.Series([], dtype="float32"),
            "low": pd.Series([], dtype="float32"),
            "close": pd.Series([], dtype="float32"),
            "volume": pd.Series([], dtype="float64"),
            "bar_count": pd.Series([], dtype="int64"),
            "shares_outstanding": pd.Series([], dtype="Float64"),
            "marketcap": pd.Series([], dtype="Float64"),
            "avg_volume_10d": pd.Series([], dtype="float64"),
            "avg_volume_20d": pd.Series([], dtype="float64"),
            "first_timestamp": pd.Series([], dtype="datetime64[us, UTC]"),
            "last_timestamp": pd.Series([], dtype="datetime64[us, UTC]"),
            "reg_first_timestamp": pd.Series([], dtype="datetime64[us, UTC]"),
            "reg_last_timestamp": pd.Series([], dtype="datetime64[us, UTC]"),
            "pm_volume": pd.Series([], dtype="float64"),
            "ah_volume": pd.Series([], dtype="float64"),
            "is_ticker_alias_duplicate": pd.Series([], dtype="bool"),
        }
    )


def test_trading_days_skip_weekends_and_holidays_and_include_the_last_needed_day() -> None:
    days = synthdata.trading_days("2024-02-15", "2024-03-06")
    assert pd.Timestamp("2024-02-19") not in days, "Presidents Day is a holiday"
    assert pd.Timestamp("2024-02-17") not in days, "a Saturday"
    assert pd.Timestamp("2024-03-05") in days


def test_generated_rows_satisfy_every_invariant_and_match_the_template_dtypes() -> None:
    template = _template()
    days = synthdata.trading_days("2022-12-01", "2023-02-28")
    df = synthdata.generate(days, synthdata.symbols(4), 7, template)
    assert synthdata.check(df, {"CPIX"}, {"AAPL"}) == []
    assert list(df.columns) == list(template.columns)
    assert df.dtypes.to_dict() == template.dtypes.to_dict()
    assert len(df) == len(days) * 4 and df["symbol"].nunique() == 4


def test_same_seed_same_data_different_seed_different_data() -> None:
    days = synthdata.trading_days("2022-12-01", "2022-12-31")
    a = synthdata.generate(days, synthdata.symbols(2), 1, _template())
    b = synthdata.generate(days, synthdata.symbols(2), 1, _template())
    c = synthdata.generate(days, synthdata.symbols(2), 2, _template())
    assert a.equals(b) and not a.equals(c)


def test_check_catches_each_kind_of_bad_row() -> None:
    days = synthdata.trading_days("2022-12-01", "2022-12-20")
    good = synthdata.generate(days, synthdata.symbols(2), 1, _template())
    assert synthdata.check(good, set(), set()) == []
    bad = good.copy()
    bad.loc[0, "high"] = 0.0001
    assert "high is below" in " ".join(synthdata.check(bad, set(), set()))
    bad = good.copy()
    bad.loc[0, "symbol"] = "AAPL"
    assert synthdata.check(bad, set(), set())
    assert "collides" in " ".join(synthdata.check(good, {"ZQAA"}, set()))
    bad = good.copy()
    bad.loc[1, "marketcap"] = 1.0
    assert "marketcap" in " ".join(synthdata.check(bad, set(), set()))
    dup = pd.concat([good, good.iloc[:1]], ignore_index=True)
    assert "duplicate" in " ".join(synthdata.check(dup, set(), set()))


def test_build_copies_the_base_untouched_and_adds_files_after_it(tmp_path: Path) -> None:
    base = tmp_path / "v_base" / "root"
    real = base / synthdata.AGG_SUBDIR / "2022"
    real.mkdir(parents=True)
    seed_rows = synthdata.generate(
        synthdata.trading_days("2022-11-01", "2022-11-30"), ["CPIX"], 3, _template()
    ).assign(symbol="CPIX")
    seed_rows.to_parquet(real / "ohlcv_2022-11.parquet", index=False)
    before = (real / "ohlcv_2022-11.parquet").read_bytes()
    directory = tmp_path / "dir.parquet"
    pd.DataFrame({"symbol": ["AAPL"], "security_name": ["x"], "is_etf": [False]}).to_parquet(
        directory
    )
    out = tmp_path / "v_new" / "root"
    manifest = synthdata.build(base, out, "2022-12-01", "2023-01-31", 2, 5, directory)
    assert (out / synthdata.AGG_SUBDIR / "2022" / "ohlcv_2022-11.parquet").read_bytes() == before
    assert manifest["files_written"] == ["ohlcv_2022-12.parquet", "ohlcv_2023-01.parquet"]
    assert (out / synthdata.DIRECTORY_REL).exists() and manifest["directory_copied"]
    assert manifest["newest_date"] == "2023-01-31"
    with pytest.raises(synthdata.SynthError, match="already exists"):
        synthdata.build(base, out)
    assert (tmp_path / "v_new" / "SYNTH_MANIFEST.json").exists()


@pytest.mark.skipif(not VENV_PYTHON.exists(), reason="fixture venv not built")
def test_the_suite_also_passes_under_the_fixture_venv_python() -> None:
    """A plain-python harness run skips these tests; run them under the interpreter with pandas."""
    if sys.executable == str(VENV_PYTHON.resolve()) or "pandas" in sys.modules:
        pytest.skip("already running under an interpreter with pandas")
    done = subprocess.run(
        [str(VENV_PYTHON), "-m", "pytest", "-q", "-p", "no:cacheprovider", str(Path(__file__))],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stdout[-800:]
