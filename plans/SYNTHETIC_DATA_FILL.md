# Synthetic data fill — plan

**Status 2026-09-30: D1-D5 done for the two measured gaps.** Decisions A and B were taken at their
recommended values (real public Nasdaq directory; measured gaps first). Extends `PLAN.md` §4.4 (data and services) and the
open Phase 2 item. Owner decisions are marked **[O]**.

## 1. Goal

The sandbox's data root today is the golden smoke slice: five real symbols, daily aggregates for
2020-01 → 2022-11, intraday bars for 2021-12 → 2022-11, halts, insider transactions, Form 4 footnotes,
market context, and a system DB initialised by the fixture's own migrations. That is enough for an
`insider_cluster` backtest (248 days, 5 trades), but code that reads anything else finds nothing, and
some tests fail for that reason alone. Fill the gaps so agents see a data root shaped like the real one,
**without adding licensed vendor data**.

## 2. What the gaps cost today (measured on fixture v2)

| Known-red tests | Why | What would fix it |
|---|---|---|
| 7 in `tests/integration/test_backtest_edge_cases.py` | The test builds its own intraday data but reads daily aggregates from the **default** path; the staleness check needs coverage through 2024-03-05, and the slice ends 2022-11-30 | Aggregates at the default path whose newest date is on or after the requested range |
| 3 `test_fails_excluded_security_type` (risk-premium research scripts) | `is_excluded_security_type` reads `FUNDAMENTALS_DIR/cache/nasdaq_symbol_directory.parquet` (`symbol`, `security_name`, `is_etf`), absent from the slice | The directory file |

Beyond the red tests, about 50 data locations are defined in `config/paths.py` (Sharadar caches, SEC
filings, news, float, borrow, SSR, FTD and more). Which of them trials and tests actually touch is not
known yet: step D1 measures it.

## 3. Principles

1. **Real where public and unlicensed, synthetic where licensed.** The Nasdaq symbol directory is a free
   public file, so the real one can be used **[O] decision A**. Vendor data (Databento, Alpaca, Sharadar)
   is never copied; it is generated.
2. **Schema from the real files, values from a generator.** The generator reads only column names,
   dtypes and partition layout from real files (no values), then produces rows that satisfy the code's
   own invariants: OHLC consistency, positive volume, `marketcap = shares × as-traded price` (a domain
   invariant), rolling ADV computed from the generated history, the trading calendar from `utils`.
3. **Synthetic symbols cannot collide with real ones.** A reserved prefix (for example `ZQ` + 2 letters,
   checked against the real directory at build time) so no synthetic row can be mistaken for a real
   symbol, and the real five-symbol slice stays byte-identical.
4. **Deterministic.** One seed per fixture version; the data root has its own tree hash in the manifest.
5. **No change to existing results.** The `insider_cluster` backtest over the slice window must give the
   same 5 trades and the same ending capital ($10,320.36) after the fill.

## 4. Steps

| Step | Work | Acceptance |
|---|---|---|
| D1 | **Inventory** what code tries to read under the data root: run the test suite and the realism-study tasks once with a Python audit hook (`sys.addaudithook`, event `open`) loaded only for this run, logging every missing path under `/mnt/ParamoStorage/trading` | A ranked list of missing paths with the tests and trials that touched them |
| D2 | **Schema capture** from the real data root: for each needed location, column names, dtypes and file layout only | A committed `schemas.json`; a test that it contains no values |
| D3 | **Generator** (`bench/fixture/synthdata.py`): aggregates from 2022-12 through the fixture's source-commit date for a small set of synthetic symbols, the Nasdaq directory (real or synthetic per decision A), then the next locations in D1's ranking | Invariant checks pass on every generated table; reserved-prefix check passes |
| D4 | **Rebuild the data root** as a new version (`data/v3/root`) and rerun | The 10 data-caused red tests are green; no other test changes status; the slice backtest result is unchanged; two builds give the same tree hash |
| D5 | **Document** in `KNOWN_RED.md` and `PLAN.md` | The known-red list shrinks by exactly the tests D4 fixed |

**[O] decision B:** how far the fill goes. Recommended: D1–D4 for the two known gaps first (small, fixes
10 tests), then fill further only where D1 shows trials or tests actually read.

## 5. Risks

| Risk | Mitigation |
|---|---|
| Synthetic data leaks a licensed value | The generator only reads schemas; a test asserts `schemas.json` has no row values; synthetic symbols use a reserved prefix |
| The fill changes a backtest or golden result | Acceptance D4: same trades and capital; the real five symbols are untouched |
| A test passes on synthetic data for the wrong reason | D4 requires "no other test changes status"; each newly green test is read once to check it exercises real logic |
| Too much fill makes the data root unlike the real machine in scale | Fill is driven by D1's measured needs, not by the full list in `paths.py` |
| The audit hook changes behaviour | It runs only in the D1 inventory run, never in a trial |

## 6. Owner decisions

- **A.** Use the real public Nasdaq symbol directory, or a synthetic one? Recommended: the real one; it is
  public, and the three tests check real ETF classification.
- **B.** Fill only the measured gaps first, or a broader fill? Recommended: measured gaps first.


## 7. Results (2026-09-30)

- **D1 inventory** (`bench/fixture/audit_reads.py`, an audit hook loaded only for the run): the whole suite in
  the sandbox produced 74 missing-path events across 12 test files and 8 distinct paths, mostly runtime state
  files the tests create themselves (`runtime/*`, a lock, a temp parquet) plus `sharadar/sf1` and `actions`.
  **Limit:** the hook sees Python-level opens and directory listings, not pyarrow's C++ reads or stat checks, so
  it missed both known gaps; treat it as a floor. The two gaps came from the failing tests' own errors.
- **D3/D4** (`bench/fixture/synthdata.py`; data root `fixtures/paramo/data/v3/root`): six symbols
  `ZQAA`..`ZQAF` (prefix checked against the real directory's 13,136 symbols, no collision), seeded random-walk
  daily rows from 2022-12-01 to 2024-03-06 (1,890 rows, 16 monthly files), dtypes taken from a real slice file
  (schema only), every invariant checked at build (OHLC, positive volume, marketcap = shares x close, rolling
  ADV, unique keys), plus the real public Nasdaq symbol directory. Seed and counts in
  `versions/v2/DATA_V3_MANIFEST.json`.
- **Acceptance met:** the suite's failing set dropped from 22 to 12; the 10 data-caused tests are green
  (7 integration, 3 security-type) and none turned red; the `insider_cluster` backtest over the slice window is
  unchanged (5 trades, $10,320.36); the real slice files are byte-identical in v3; the build is deterministic
  (same seed, same data; a test asserts it).
- **Not done (none measured as needed):** further fill. The remaining 12 red tests are the 11 doc/index checks
  that cite removed files and one test written against real machine state.
