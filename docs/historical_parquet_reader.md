# Historical Parquet Reader

PR 5 adds the first local historical market-data file boundary for V2. The
reader accepts local Parquet files, validates the small set of columns needed to
construct `MarketRecord`, and normalizes rows into the internal contract.

## Purpose

Historical market truth will come from official Databento historical Parquets
or Databento DBN converted to Parquet using Databento tooling. This reader is
only the local file boundary for those future files. It does not call Databento
APIs, decode DBN, write QuestDB records, build features, calculate labels, train
models, or support live trading.

QuestDB remains a bot ledger only. It must not be used as a historical
market-data warehouse, and it must not be used to generate training Parquets.

## Fake Test Parquets

These fake Parquet files test reader mechanics and common timestamp shapes only.
They are not official Databento schema fixtures. Real Databento schema mapping
will be validated later using ignored local samples.

Unit tests generate tiny fake Parquet files at runtime under temporary
directories. They cover generic columns and Databento-like timestamp/column
shapes, but they do not prove exact Databento production schema accuracy.

Do not commit real Databento data, DBN files, DBN archives, or Parquet files.
Future local samples should live in ignored folders such as:

```text
data/raw/databento/
data/parquet_market/
```

## Timestamp Handling

All timestamps entering `MarketRecord` are normalized to timezone-aware UTC.
The reader accepts timezone-aware `datetime` values, UTC ISO strings, PyArrow
values after batch conversion to Python values, and integer nanoseconds since
the Unix epoch. Integer timestamp values are treated as nanoseconds only.
Seconds, milliseconds, and microseconds are not inferred in PR 5.

Naive datetimes are rejected.

## Field Mapping

The default mapping uses generic test column names such as `event_time`,
`ticker`, `record_type`, `price`, `bid_price`, and `ask_price`. This is not an
exact Databento mapping. Later PRs may add or revise mappings after inspecting
real ignored Databento Parquets and DBN-to-Parquet output.

`local_receive_time` defaults to `None` for historical records unless a caller
explicitly maps a column. Historical files generally do not contain the local
live bot receive time, and PR 5 does not fake one.

Midprice and spread are not computed by this reader. The canonical feature
builder is responsible for deriving these from `bid_price` and `ask_price` so
historical and live paths use the same feature logic.

## Reader Functions

Use `iter_market_records_from_parquet()` for large historical files and future
feature-builder integration. It reads PyArrow batches and yields normalized
records one at a time.

`read_market_records_from_parquet()` loads records into a list and is intended
for small test files and inspection only.

`read_parquet_table()` loads a full Parquet file into memory. It is for small
files, tests, and inspection only.

The `limit` argument controls the number of records yielded or returned. It does
not guarantee that only that many rows are read from disk because PyArrow reads
in batches.

## Inspection

Inspect a local ignored Parquet sample with:

```powershell
python scripts/inspect_historical_parquets.py --path data/parquet_market/sample.parquet --limit 5
```

The script prints the path, row count, column names, schema, normalized record
count, and the first normalized records as JSON-safe dictionaries.

## Validation

Run the historical Parquet check without real market data:

```powershell
python scripts/check_historical_parquet.py
```

Run the full local validation suite:

```powershell
python scripts/check_environment.py
python scripts/check_config.py
python scripts/check_contracts.py
python scripts/check_fixtures.py
python scripts/check_historical_parquet.py
python -m pytest
powershell -ExecutionPolicy Bypass -File scripts/run_tests.ps1
```
