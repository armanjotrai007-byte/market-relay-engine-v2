# handoff.md — Trading System V2 Clean Handoff

## Current Status

Repository: `armanjotrai007-byte/market-relay-engine-v2`

Canonical source of truth: GitHub `main`.

Current active PR:

- **PR 7 — Canonical Feature Builder V1**
- Branch: `pr7-canonical-feature-builder-v1`
- PR: `https://github.com/armanjotrai007-byte/market-relay-engine-v2/pull/7`
- Head SHA: `635f938651bb865a87be2282695a10d97484bf58`
- Status: open and ready for review
- Next PR after merge: **PR 8 — Historical/Live Feature Parity Tests**

Latest confirmed merged base before PR 7:

- **PR 6 merge commit:** `a3c5f8a5156e9b2a2789db50a9d4d638f7cc3c2a`

---

## Project Summary

This repo builds a local AI-assisted trading research and paper/live execution system.

Core flow:

```text
Databento market data
→ normalized MarketRecord
→ canonical feature builder
→ model signal
→ deterministic risk filter
→ Alpaca paper/live execution
→ QuestDB bot ledger
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

### PR 1 — Clean Repo Skeleton

Added:

- base repo structure
- Python package layout
- docs/config placeholders
- `.env.example`
- `.gitignore`
- basic tests
- environment check
- PowerShell test runner

Did not add external APIs, live trading, model code, QuestDB writes, or broker logic.

---

### PR 2 — Config Organization and Validation

Added:

- `config/symbols.yaml`
- `config/context_sources.yaml`
- `config/risk_limits.yaml`
- `config/questdb.yaml`
- `config/model_config.yaml`
- `config/calendar_events.yaml`
- `config/execution.yaml`
- config loader
- `scripts/check_config.py`
- config tests
- `docs/configuration.md`

Purpose:

- safe defaults
- paper-only execution by default
- context sources disabled/development-safe by default
- QuestDB marked bot-ledger-only

---

### PR 3 — Core Contracts + Timestamp Standards

Added typed contracts for:

- `MarketRecord`
- `FeatureSnapshot`
- `ModelSignal`
- `RiskDecision`
- `ContextIndicatorSnapshot`
- `ContextAIEvent`
- `ContextFlag`
- `OrderEvent`
- `FillEvent`
- `TradeOutcome`
- `LatencyMetric`
- `SystemHealthEvent`

Added:

- UTC timestamp helpers
- run/session/trace ID helpers
- JSON serialization helpers
- logging context helper
- `scripts/check_contracts.py`
- `docs/data_contracts.md`

Purpose:

Define stable record shapes for the whole system.

---

### PR 4 — Reusable Test Fixtures and Sample Records

Added reusable fake fixture factories under:

```text
tests/fixtures/
```

Added scenarios:

- approved oil trade
- blocked defense trade
- reduced-size context-risk trade
- latency/slippage warning
- stale context block

Added:

- `scripts/check_fixtures.py`
- `docs/testing_fixtures.md`

Purpose:

Give future PRs consistent fake data built from PR 3 contracts.

No real Databento data was added.

---

### PR 5 — Historical Databento Parquet Reader Stub

Added:

- `src/market_relay_engine/market_data/historical_parquet.py`
- `scripts/inspect_historical_parquets.py`
- `scripts/check_historical_parquet.py`
- `docs/historical_parquet_reader.md`
- Parquet reader tests

Purpose:

Create the local historical Parquet boundary:

```text
official Databento historical Parquet
→ MarketRecord
```

Important behavior:

- tests use tiny generated fake Parquet files
- fake Parquets are not official Databento schema proof
- integer nanosecond timestamps are supported
- historical records do not fake `local_receive_time`
- reader does not compute features

---

### PR 6 — DBN Inspection Utility

Merged into `main`.

Added:

- `src/market_relay_engine/market_data/dbn_inspector.py`
- `scripts/inspect_dbn_file.py`
- `scripts/check_dbn_inspector.py`
- `docs/dbn_inspection.md`
- DBN inspector tests

Purpose:

Inspect local Databento DBN files/folders safely without committing raw data.

Supported:

- `.dbn`
- `.dbn.zst`
- Databento batch/job folders
- sidecar JSON files:
  - `condition.json`
  - `manifest.json`
  - `metadata.json`

Important behavior:

- file-info-only mode does not require Databento package
- record preview is optional and bounded
- schema values are `schema_hint`, not guaranteed truth
- sidecar schema hints are preferred over filename hints
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

## Current PR

### PR 7 — Canonical Feature Builder V1

Open PR:

```text
https://github.com/armanjotrai007-byte/market-relay-engine-v2/pull/7
```

Branch:

```text
pr7-canonical-feature-builder-v1
```

Purpose:

Create the first canonical feature builder:

```text
MarketRecord
→ FeatureSnapshot
```

Added:

- `src/market_relay_engine/market_data/feature_builder.py`
- `scripts/check_feature_builder.py`
- `docs/feature_builder.md`
- `tests/unit/test_feature_builder.py`

Modified:

- `README.md`
- `handoff.md`
- `scripts/check_environment.py`
- `scripts/run_tests.ps1`

Important PR 7 behavior:

- `FeatureSnapshot` contract is unchanged.
- V1 features live inside `FeatureSnapshot.features`.
- `V1_FEATURE_KEYS` defines the stable feature dictionary schema.
- `FeatureBuilderConfig` defaults:
  - `lookback_window_seconds=60`
  - `feature_version="feature_v1"`
  - `max_records_per_ticker=50000`
- `FeatureBuilder.update(record)` processes caller order for live-style use.
- `build_feature_snapshot(records, ...)` sorts by `event_time` for batch/test convenience.
- Per-ticker rolling windows use `max_event_time_seen`, so late records cannot move the window backward.
- Update order:
  1. validate
  2. append
  3. update max event time
  4. prune by time
  5. apply record cap
- Non-finite numbers are normalized to `None`.
- Features are JSON-safe.
- Unrecognized records contribute only to `record_count_window` unless they contain usable trade/quote fields.

PR 7 explicitly does not add:

- DBN parsing
- Databento API
- live feed
- QuestDB writes
- model training/inference
- risk logic
- Alpaca
- live trading
- heavy dependencies

PR 7 validation reported by Codex:

```text
check_environment.py PASS
check_config.py PASS
check_contracts.py PASS
check_fixtures.py PASS
check_historical_parquet.py PASS
check_dbn_inspector.py PASS
check_feature_builder.py PASS
pytest: 157 passed
run_tests.ps1 PASS
```

Before merge, run validation on the server laptop.

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
.\.venv\Scripts\python.exe -m pytest
powershell -ExecutionPolicy Bypass -File scripts/run_tests.ps1
```

After PR 8 is added, also run:

```powershell
.\.venv\Scripts\python.exe scripts/check_feature_parity.py
```

---

## PR 8 Guidance

Next PR:

```text
PR 8 — Historical/Live Feature Parity Tests
```

Purpose:

Prove historical-style and live-style feature paths use the same canonical feature builder.

Important rule:

```text
build_feature_snapshot() sorts by event_time.
FeatureBuilder.update() processes arrival order and does not sort.
```

Therefore, PR 8 parity tests must compare batch and stateful paths using event-time-ordered inputs unless the test is specifically about out-of-order behavior.

PR 8 should not add:

- Databento parsing
- real DBN/Parquet usage
- QuestDB writes
- model training/inference
- risk logic
- Alpaca
- live trading

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
scripts/run_tests.ps1
```

Docs:

```text
docs/data_contracts.md
docs/testing_fixtures.md
docs/historical_parquet_reader.md
docs/dbn_inspection.md
docs/feature_builder.md
docs/configuration.md
```

---

## Next Steps

1. Review PR 7.
2. Run PR 7 validation on the server laptop.
3. Merge PR 7 if validation and review are clean.
4. Start PR 8 — Historical/Live Feature Parity Tests.
