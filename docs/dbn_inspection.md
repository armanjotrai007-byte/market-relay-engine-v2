# DBN Inspection Utility

PR 6 adds a small local inspection boundary for Databento DBN files and
Databento batch folders. It is inspection only. It does not call Databento
cloud APIs, require API keys for file-info inspection, write QuestDB records,
convert DBN to Parquet, build features, create labels, train models, run model
inference, connect to Alpaca, or support live trading.

QuestDB remains the bot ledger only. It must not be used as a historical
market-data warehouse and must not be used to generate training Parquets.

## Local Folder Convention

Real local DBN samples should stay in ignored local data folders:

```text
data/raw/databento/<sample-name>/<databento-job-folder>/*.dbn
```

Example:

```text
data/raw/databento/LTN 2025-05-14 DBN/XNAS-20260515-5GXPNM33CY/xnas-itch-20260513.trades.dbn
```

Databento batch/job folders may also include nearby sidecar files:

```text
condition.json
manifest.json
metadata.json
```

Raw `.dbn`, `.dbn.zst`, Parquet, and other market-data files must not be
committed to GitHub.

## What The Inspector Reports

File-info inspection reports local file metadata only:

- path, file name, DBN suffix, file size, and parent folder
- nearby sidecar JSON file names
- safe sidecar summaries, including top-level keys and selected obvious values
- a best-effort `schema_hint` with `schema_hint_source`

Schema hints are intentionally named as hints because they are not production
schema validation. Sidecar schema fields are preferred when present. Otherwise
the inspector uses a simple filename hint such as:

```text
xnas-itch-20260513.trades.dbn -> trades
```

If no obvious hint exists, the schema fields stay empty.

Folder inspection recursively finds `.dbn` and `.dbn.zst` files, counts job
folders, counts sidecars, sums DBN bytes, lists schema hints, and displays only
the first files requested by `--max-files`.

## Record Preview

File-info-only mode works without the optional Databento Python package and
does not read DBN record contents.

Record-level preview may require the optional Databento package. If it is not
installed, preview fails clearly while file/folder inspection still works. If
Databento is installed, preview opens the local file with `DBNStore.from_file`
and uses direct bounded iteration. It does not call `replay()`, `to_df()`,
`to_ndarray()`, `to_json()`, `to_csv()`, or any Databento cloud/API client.

Preview output is conservative: record count previewed, record type names,
field names when safely available, and short safe previews.

PR 6 does not map DBN records to `MarketRecord`. Exact DBN-to-contract mapping
happens later after local inspection.

## Commands

Inspect a local Databento batch folder without Databento installed:

```powershell
python scripts/inspect_dbn_file.py --path "data/raw/databento/LTN 2025-05-14 DBN" --file-info-only
```

Limit folder output:

```powershell
python scripts/inspect_dbn_file.py --path "data/raw/databento/LTN 2025-05-14 DBN" --file-info-only --max-files 10
```

Inspect one DBN file without reading records:

```powershell
python scripts/inspect_dbn_file.py --path "data/raw/databento/LTN 2025-05-14 DBN/XNAS-20260515-5GXPNM33CY/xnas-itch-20260513.trades.dbn" --file-info-only
```

Attempt a bounded record preview when Databento is installed:

```powershell
python scripts/inspect_dbn_file.py --path "data/raw/databento/LTN 2025-05-14 DBN/XNAS-20260515-5GXPNM33CY/xnas-itch-20260513.trades.dbn" --limit 5
```

Run the DBN inspector health check without real DBN files:

```powershell
python scripts/check_dbn_inspector.py
```

Run the full validation suite:

```powershell
python scripts/check_environment.py
python scripts/check_config.py
python scripts/check_contracts.py
python scripts/check_fixtures.py
python scripts/check_historical_parquet.py
python scripts/check_dbn_inspector.py
python -m pytest
powershell -ExecutionPolicy Bypass -File scripts/run_tests.ps1
```
