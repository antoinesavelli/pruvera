"""Generate the synthetic part of a fixture's data root: aggregates that obey the code's invariants.

The schema (column names, dtypes, partition layout) comes from a real slice file; no value is read
from real data. Symbols carry a reserved prefix no real symbol has, so a synthetic row is never
mistaken for a real one, and the real slice is copied byte for byte. One seed gives one data root.
Needs pandas and numpy: run it with the fixture venv's python. Depends on: pandas, numpy, pyarrow.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from pandas.tseries.holiday import USFederalHolidayCalendar

PREFIX = "ZQ"
AGG_SUBDIR = Path("backtesting data/technical indicators/ohlcv")
DIRECTORY_REL = Path("backtesting data/fundamentals/cache/nasdaq_symbol_directory.parquet")
ET_OPEN_UTC_HOUR, ET_CLOSE_UTC_HOUR = (
    14,
    21,
)  # regular session 09:30-16:00 ET in UTC, rounded to the hour


class SynthError(RuntimeError):
    """The generated data breaks an invariant, or collides with real symbols."""


def trading_days(start: str, end: str) -> pd.DatetimeIndex:
    """Weekdays that are not US federal holidays (close enough to the exchange calendar)."""
    days = pd.bdate_range(start, end)
    holidays = USFederalHolidayCalendar().holidays(start, end)
    return days.difference(holidays)


def symbols(n: int) -> list[str]:
    """`n` reserved-prefix symbols: ZQAA, ZQAB, ..."""
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return [f"{PREFIX}{letters[i // 26 % 26]}{letters[i % 26]}" for i in range(n)]


def generate(
    days: pd.DatetimeIndex, syms: list[str], seed: int, template: pd.DataFrame
) -> pd.DataFrame:
    """One row per (day, symbol), shaped like `template`, from a seeded random walk."""
    rng = np.random.default_rng(seed)
    frames = []
    for sym in syms:
        n = len(days)
        close = 5.0 * np.exp(np.cumsum(rng.normal(0.0002, 0.03, n)))
        open_ = close * np.exp(rng.normal(0, 0.01, n))
        high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.01, n)))
        low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.01, n)))
        volume = np.round(rng.lognormal(11.5, 0.6, n))
        shares = float(rng.integers(5_000_000, 80_000_000))
        first = days.tz_localize("UTC") + pd.Timedelta(hours=ET_OPEN_UTC_HOUR, minutes=30)
        last = days.tz_localize("UTC") + pd.Timedelta(hours=ET_CLOSE_UTC_HOUR)
        frames.append(
            pd.DataFrame(
                {
                    "date": days,
                    "symbol": sym,
                    "open": open_,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                    "bar_count": rng.integers(200, 390, n),
                    "shares_outstanding": shares,
                    "marketcap": shares * close,
                    "avg_volume_10d": pd.Series(volume)
                    .rolling(10, min_periods=1)
                    .mean()
                    .to_numpy(),
                    "avg_volume_20d": pd.Series(volume)
                    .rolling(20, min_periods=1)
                    .mean()
                    .to_numpy(),
                    "first_timestamp": first,
                    "last_timestamp": last,
                    "reg_first_timestamp": first,
                    "reg_last_timestamp": last,
                    "pm_volume": np.round(volume * 0.05),
                    "ah_volume": np.round(volume * 0.03),
                    "is_ticker_alias_duplicate": False,
                }
            )
        )
    out = pd.concat(frames, ignore_index=True).sort_values(["date", "symbol"], ignore_index=True)
    return out[list(template.columns)].astype(template.dtypes.to_dict())


def check(df: pd.DataFrame, real_symbols: set[str], directory_symbols: set[str]) -> list[str]:
    """Every invariant the fixture relies on; an empty list means the table is sound."""
    ends = df[["open", "close"]]
    cap = df["shares_outstanding"].astype(float) * df["close"].astype(float)
    holds = [
        (df["symbol"].str.startswith(PREFIX).all(), "a symbol lacks the reserved prefix"),
        (
            not set(df["symbol"]) & (real_symbols | directory_symbols),
            "a synthetic symbol collides with a real one",
        ),
        ((df["high"] >= ends.max(axis=1) - 1e-4).all(), "high is below open or close"),
        (
            (df["low"] <= ends.min(axis=1) + 1e-4).all() and (df["low"] > 0).all(),
            "low is above open or close, or not positive",
        ),
        (
            (df["volume"] > 0).all() and (df["bar_count"] > 0).all(),
            "volume or bar_count is not positive",
        ),
        (
            np.allclose(df["marketcap"].astype(float), cap, rtol=1e-3),
            "marketcap is not shares_outstanding x close",
        ),
        (
            (df["first_timestamp"] <= df["last_timestamp"]).all(),
            "first_timestamp is after last_timestamp",
        ),
        (not df.duplicated(["date", "symbol"]).any(), "duplicate (date, symbol) rows"),
    ]
    return [why for ok, why in holds if not ok]


def build(
    base_root: Path,
    out_root: Path,
    start: str = "2022-12-01",
    end: str = "2024-03-06",
    n_symbols: int = 6,
    seed: int = 1,
    directory_src: Path | None = None,
) -> dict[str, Any]:
    """Copy `base_root` to `out_root`, add synthetic aggregates and the public directory file."""
    if out_root.exists():
        raise SynthError(f"already exists: {out_root}")
    shutil.copytree(base_root, out_root)
    agg_dir = out_root / AGG_SUBDIR
    files = sorted(agg_dir.glob("*/ohlcv_*.parquet"))
    template = pd.read_parquet(files[-1]).iloc[0:0]  # dtypes and columns only, never values
    real = set(pd.concat([pd.read_parquet(f, columns=["symbol"]) for f in files])["symbol"])
    directory_symbols: set[str] = set()
    if directory_src is not None:
        (out_root / DIRECTORY_REL).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(directory_src, out_root / DIRECTORY_REL)
        directory_symbols = set(pd.read_parquet(directory_src, columns=["symbol"])["symbol"])
    df = generate(trading_days(start, end), symbols(n_symbols), seed, template)
    problems = check(df, real, directory_symbols)
    if problems:
        shutil.rmtree(out_root)
        raise SynthError("; ".join(problems))
    written = []
    for (year, month), part in df.groupby([df["date"].dt.year, df["date"].dt.month]):
        target = agg_dir / str(year) / f"ohlcv_{year}-{month:02d}.parquet"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise SynthError(f"would overwrite a real slice file: {target.name}")
        part.to_parquet(target, index=False)
        written.append(target.name)
    manifest = {
        "seed": seed,
        "symbols": symbols(n_symbols),
        "start": start,
        "end": end,
        "rows": len(df),
        "files_written": written,
        "directory_copied": directory_src is not None,
        "newest_date": str(df["date"].max().date()),
    }
    (out_root.parent / "SYNTH_MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main(argv: list[str] | None = None) -> int:
    """Build a synthetic data root from a base root (needs pandas, numpy: the fixture venv)."""
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--base", type=Path, required=True, help="the real slice's data root")
    parser.add_argument(
        "--out", type=Path, required=True, help="the new data root (must not exist)"
    )
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--symbols", type=int, default=6)
    parser.add_argument("--directory", type=Path, help="the public Nasdaq symbol directory parquet")
    args = parser.parse_args(argv)
    manifest = build(
        args.base, args.out, n_symbols=args.symbols, seed=args.seed, directory_src=args.directory
    )
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
