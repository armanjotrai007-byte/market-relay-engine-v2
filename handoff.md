# handoff.md - Trading System V2 Clean Handoff

## Current Status

Repository: `armanjotrai007-byte/market-relay-engine-v2`

Canonical source of truth: GitHub `main`.

Current active PR:

- **PR 8 - Historical/Live Feature Parity Tests**
- Branch: `pr8-historical-live-feature-parity`
- Purpose: prove historical-style and live-style feature paths use the same canonical feature builder for equivalent event-time-ordered inputs.
- Next PR after merge: **PR 9 - Cost Model V1**

Latest confirmed merged base before PR 8:

- **PR 7 merge commit:** `bd561148de4de5ddd4025959b3bffa74cb2ffa8a`

---

## Project Summary

This repo builds a local AI-assisted trading research and paper/live execution system.

Core flow:

```text
Databento market data
-> normalized MarketRecord
-> canonical feature builder
-> model signal
-> deterministic risk filter
-> Alpaca paper/live execution
-> QuestDB bot ledger
```

QuestDB is only the bot ledger. It must not be used as a historical market-data warehouse.

Historical market truth comes from official Databento historical DBN/Parquet files, not QuestDB.

---

## Non-Negotiable Rules

1. GitHub is the official project filesystem.
2. Test every PR on the server laptop before merging.
3. Keep raw Databento files local and ignored.
4. Do not commit `.dbn`, `.dbn.zst`, `.parquet`, logs, `.env`, or API keys.
5. Do not use QuestDB as historical market-data storage.
6. Use one canonical feature builder for historical and live paths.
7. AI context may produce structured risk flags only; it must not directly trade.
8. The deterministic Python risk filter is the final gate before execution.
9. Alpaca starts as paper trading only.
10. Keep PRs small, simple, testable, and reviewable.

---

## Completed PRs

### PR 1 - Clean Repo Skeleton

Added base repo structure, Python package layout, docs/config placeholders, `.env.example`, `.gitignore`, basic tests, environment check, PowerShell test runner, and empty tracked data/log directories.

Did not add external APIs, live trading, model code, QuestDB writes, or broker logic.

---

### PR 2 - Config Organization and Validation

Added YAML config organization, config loader, config validation script/tests, and `docs/configuration.md`.

Purpose:

- safe defaults
- paper-only execution by default
- context sources disabled/development-safe by default
- QuestDB marked bot-ledger-only

---

### PR 3 - Core Contracts + Timestamp Standards

Added typed contracts for market records, feature snapshots, model signals, risk decisions, context records, execution records, ledger records, and system health records.

Added UTC timestamp helpers, run/session/trace ID helpers, JSON serialization helpers, logging context helper, `scripts/check_contracts.py`, and `docs/data_contracts.md`.

Purpose: define stable record shapes for the whole system.

---

### PR 4 - Reusable Test Fixtures and Sample Records

Added reusable fake fixture factories under `tests/fixtures/` and scenario fixtures for approved, blocked, reduced-size, latency/slippage, and stale-context cases.

Purpose: give future PRs consistent fake data built from PR 3 contracts. No real Databento data was added.

---

### PR 5 - Historical Databento Parquet Reader Stub

Added `src/market_relay_engine/market_data/historical_parquet.py`, local inspection/check scripts, docs, and Parquet reader tests.

Purpose:

```text
official Databento historical Parquet
-> MarketRecord
```

Important behavior:

- tests use tiny generated fake Parquet files
- fake Parquets are not official Databento schema proof
- integer nanosecond timestamps are supported
- historical records do not fake `local_receive_time`
- reader does not compute features

---

### PR 6 - DBN Inspection Utility

Merged into `main`.

Added DBN file/folder inspection helpers, CLI, check script, docs, and tests.

Purpose: inspect local Databento DBN files/folders safely without committing raw data.

Important behavior:

- supports `.dbn`, `.dbn.zst`, batch/job folders, and sidecar JSON files
- file-info-only mode does not require Databento package
- record preview is optional and bounded
- schema values are `schema_hint`, not guaranteed truth
- no DBN files are committed
- no live Databento, QuestDB writes, model logic, or trading logic

Real local inspection confirmed the sample folder contained:

```text
13 DBN files
13 job folders
39 sidecar files
schemas: trades, mbp-1, tbbo, bbo-1s, bbo-1m, ohlcv-1s, ohlcv-1m, ohlcv-1h, ohlcv-1d, definition, statistics, status, imbalance
```

---

### PR 7 - Canonical Feature Builder V1

Merged into `main`.

Added canonical `MarketRecord -> FeatureSnapshot` builder, V1 feature keys, quote normalization, rolling window behavior, check script, docs, and tests.

Important PR 7 behavior:

- `FeatureSnapshot` contract is unchanged.
- V1 features live inside `FeatureSnapshot.features`.
- `FeatureBuilder.update(record)` processes caller order for live-style use.
- `build_feature_snapshot(records, ...)` sorts by `event_time` for batch/test convenience.
- Per-ticker rolling windows use `max_event_time_seen`, so late records cannot move the window backward.
- Non-finite numbers are normalized to `None`.
- Features are JSON-safe.

PR 7 explicitly did not add DBN parsing, Databento API, live feed, QuestDB writes, model training/inference, risk logic, Alpaca, live trading, or heavy dependencies.

---

## Current PR

### PR 8 - Historical/Live Feature Parity Tests

Branch:

```text
pr8-historical-live-feature-parity
```

Purpose:

Prove historical-style and live-style feature paths use the same canonical feature builder for equivalent event-time-ordered inputs.

Added:

- `src/market_relay_engine/market_data/feature_parity.py`
- `scripts/check_feature_parity.py`
- `docs/feature_parity.md`
- `tests/unit/test_feature_parity.py`

Important PR 8 behavior:

- Historical helper sorts normalized `MarketRecord` inputs by `event_time`.
- Live helper processes caller order and does not sort.
- Live helper does not reject out-of-order event times because PR 7 `FeatureBuilder.update(record)` supports live-style arrival order.
- Formal parity assertions compare equivalent event-time-ordered inputs.
- Same-timestamp records are allowed, but deterministic parity requires the same relative input order for records with equal `event_time`.
- Semantic comparison checks market-derived `snapshot_time` and feature values, but ignores generated `feature_snapshot_id`.

PR 8 explicitly does not add Databento parsing, real DBN/Parquet usage, QuestDB writes, model training/inference, risk logic, Alpaca, live trading, AI/context collectors, or heavy dependencies.

Next PR:

```text
PR 9 - Cost Model V1
```

---

## Standard Server-Laptop Validation

Run from the repo root after checking out the PR branch:

```powershell
.\.venv\Scripts\python.exe scripts/check_environment.py
.\.venv\Scripts\python.exe scripts/check_config.py
.\.venv\Scripts\python.exe scripts/check_contracts.py
.\.venv\Scripts\python.exe scripts/check_fixtures.py
.\.venv\Scripts\python.exe scripts/check_historical_parquet.py
.\.venv\Scripts\python.exe scripts/check_dbn_inspector.py
.\.venv\Scripts\python.exe scripts/check_feature_builder.py
.\.venv\Scripts\python.exe scripts/check_feature_parity.py
.\.venv\Scripts\python.exe -m pytest
powershell -ExecutionPolicy Bypass -File scripts/run_tests.ps1
```

---

## PR 8 Rule

Formal parity assertions compare historical and live outputs only when both paths receive equivalent event-time-ordered inputs. The historical helper sorts by `event_time`; the live helper processes caller order and does not reject out-of-order records.

Out-of-order live arrival is supported by PR 7's `FeatureBuilder`, but it is not the main parity condition. Same-timestamp ordering matters because equal timestamps do not define a unique order by themselves. This preserves the PR 7 batch sorting vs live arrival order distinction.

---

## Files To Know

Core contracts:

```text
src/market_relay_engine/contracts/
```

Feature builder:

```text
src/market_relay_engine/market_data/feature_builder.py
```

Historical Parquet reader:

```text
src/market_relay_engine/market_data/historical_parquet.py
```

DBN inspector:

```text
src/market_relay_engine/market_data/dbn_inspector.py
```

Fixtures:

```text
tests/fixtures/
```

Validation scripts:

```text
scripts/check_environment.py
scripts/check_config.py
scripts/check_contracts.py
scripts/check_fixtures.py
scripts/check_historical_parquet.py
scripts/check_dbn_inspector.py
scripts/check_feature_builder.py
scripts/check_feature_parity.py
scripts/run_tests.ps1
```

Docs:

```text
docs/data_contracts.md
docs/testing_fixtures.md
docs/historical_parquet_reader.md
docs/dbn_inspection.md
docs/feature_builder.md
docs/feature_parity.md
docs/configuration.md
```

---

## Next Steps

1. Review PR 8.
2. Run PR 8 validation on the server laptop.
3. Merge PR 8 if validation and review are clean.
4. Start PR 9 - Cost Model V1.
