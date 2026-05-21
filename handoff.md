# handoff.md â€” Trading System V2 Clean Handoff

## Current Status

Repository: `armanjotrai007-byte/market-relay-engine-v2`

Canonical source of truth: GitHub `main`.

Current active PR:

- **PR 10 - Label Builder for Supervised Model**
- Branch: `pr10-label-builder-for-supervised-model`
- Purpose: cost-aware label builder for supervised model training.
- Next PR after merge: **PR 11 - QuestDB Health Check / Ledger Foundation**

Latest confirmed merged base before PR 10:

- **PR 9 merge commit:** `d06c2b21bb2e5d1aa7183bb23caa6aa1b0c6770e`

Local workspace and publishing note:

- This local workspace is not a usable Git checkout: `.git` is absent, and
  `git` / `gh` were not available on PATH during PR 8 publishing.
- Recent branches may need to be created on GitHub with the GitHub connector
  instead of local git when this workspace remains connector-only.

---

## Project Summary

This repo builds a local AI-assisted trading research and paper/live execution system.

Core flow:

```text
Databento market data
â†’ normalized MarketRecord
â†’ canonical feature builder
â†’ model signal
â†’ deterministic risk filter
â†’ Alpaca paper/live execution
â†’ QuestDB bot ledger
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

### PR 1 â€” Clean Repo Skeleton

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

### PR 2 â€” Config Organization and Validation

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

### PR 3 â€” Core Contracts + Timestamp Standards

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

### PR 4 â€” Reusable Test Fixtures and Sample Records

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

### PR 5 â€” Historical Databento Parquet Reader Stub

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
â†’ MarketRecord
```

Important behavior:

- tests use tiny generated fake Parquet files
- fake Parquets are not official Databento schema proof
- integer nanosecond timestamps are supported
- historical records do not fake `local_receive_time`
- reader does not compute features

---

### PR 6 â€” DBN Inspection Utility

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

## Previous PR Context

### PR 8 - Historical/Live Feature Parity Tests

GitHub PR:

```text
https://github.com/armanjotrai007-byte/market-relay-engine-v2/pull/8
```

Branch:

```text
pr8-historical-live-feature-parity
```

Status:

```text
Open on GitHub
```

Branch head:

```text
edf750f7faf90f876b9cc0a794534be7763f3ee4
```

Purpose:

Prove historical-style and live-style feature paths use the same canonical
feature builder for equivalent event-time-ordered inputs.

Added:

- `src/market_relay_engine/market_data/feature_parity.py`
- `scripts/check_feature_parity.py`
- `docs/feature_parity.md`
- `tests/unit/test_feature_parity.py`

Updated:

- `README.md`
- `handoff.md`
- `scripts/check_environment.py`
- `scripts/run_tests.ps1`

Important PR 8 behavior:

- Historical helper sorts normalized `MarketRecord` inputs by `event_time`.
- Live helper processes caller order and does not sort.
- Live helper does not reject out-of-order event times because PR 7
  `FeatureBuilder.update(record)` supports live-style arrival order.
- Formal parity assertions compare equivalent event-time-ordered inputs.
- Same-timestamp records are allowed, but deterministic parity requires the
  same relative input order for records with equal `event_time`.
- Semantic comparison checks market-derived `snapshot_time` and feature values,
  but ignores generated `feature_snapshot_id`.
- `feature_snapshot_semantic_dict()` is testing/validation support only and is
  not a stable production API.

Feature parity helper details:

- `build_historical_style_snapshot(...)` listifies input, rejects empty input
  and multiple tickers, uses stable event-time sorting, then feeds records
  through the canonical `FeatureBuilder`.
- `build_live_style_snapshot(...)` listifies input, rejects empty input and
  multiple tickers, does not sort, does not reject out-of-order event times,
  and calls `FeatureBuilder.update(record)` in caller order.
- `assert_event_time_ordered(records)` is available for formal parity
  preconditions; it allows equal timestamps and rejects decreasing event time.
- `assert_feature_snapshots_equivalent(left, right)` compares deterministic
  semantic fields and feature values, uses tight `math.isclose()` tolerance
  for floats, rejects NaN/Infinity, and ignores generated IDs.

Tests cover:

- clean imports
- empty and multi-ticker rejection
- historical event-time sorting and stable equal-timestamp sorting
- live caller-order behavior, including out-of-order inputs that do not crash
- ordered trade-only, quote-only, mixed trade/quote, rolling-window, and
  same-timestamp parity
- `snapshot_time` comparison, source record counts, feature key matching,
  generated ID ignoring, tiny float differences, missing keys, value
  differences, non-finite values, JSON serialization, and no external-service
  dependencies.

PR 8 explicitly does not add Databento parsing, real DBN/Parquet usage, QuestDB
writes, model training/inference, risk logic, Alpaca, live trading, AI/context
collectors, or heavy dependencies.

Local validation already run for PR 8:

```text
scripts/check_feature_parity.py PASS
python -m pytest PASS, 183 passed
powershell -ExecutionPolicy Bypass -File scripts/run_tests.ps1 PASS, 183 passed
```

Next PR:

```text
PR 9 - Cost Model V1
```

---

## Previous PR

### PR 9 - Cost Model V1

Branch:

```text
pr9-cost-model-v1
```

Purpose:

Create a pure cost calculation layer that estimates whether a hypothetical
mid-to-mid expected move clears spread, round-trip slippage, size penalty,
missed-fill risk, and the minimum edge buffer.

Added:

- `src/market_relay_engine/market_data/cost_model.py`
- `scripts/check_cost_model.py`
- `docs/cost_model.md`
- `tests/unit/test_cost_model.py`

Key PR 9 decisions:

- Supported horizons are `1m`, `5m`, and `15m`.
- Expected gross move is mid-to-mid; spread is subtracted separately.
- Default round-trip slippage is `$0.02/share`.
- Default `min_edge_bps` is `1.0`.
- LIMIT_AT_MID missed-fill probabilities are horizon-specific.
- Crossed/locked books are rejected, and zero or missing spread uses fallback
  spread bps.
- BUY and SELL math are supported through existing `SignalSide`.
- First model target remains classification: `profitable_after_costs`.

Explicitly not added:

- label builder
- model training or inference
- risk engine
- Alpaca or broker execution
- QuestDB schemas or writes
- Databento API, DBN parsing, or Parquet reader changes
- live data, AI/context collectors, or live trading

Next PR:

```text
PR 10 - Label Builder for Supervised Model
```

---

## Current PR

### PR 10 - Label Builder for Supervised Model

Branch:

```text
pr10-label-builder-for-supervised-model
```

Purpose:

Create cost-aware labels for future supervised model training.

Added:

- `src/market_relay_engine/market_data/label_builder.py`
- `scripts/check_label_builder.py`
- `docs/label_builder.md`
- `tests/unit/test_label_builder.py`

Key PR 10 decisions:

- Supported label horizons are `1m`, `5m`, and `15m`.
- Labels are generated for BUY and SELL sides only.
- Forward movement is mid-to-mid and uses the PR 9 cost model.
- Regular-hours protection rejects after-hours horizon targets and forward
  observations outside 09:30-16:00 America/New_York.
- Forward price selection never uses observations before the target horizon,
  which prevents lookahead leakage.

Explicitly not added:

- model training or inference
- risk engine
- Alpaca or broker execution
- QuestDB schemas or writes
- Databento API, DBN parsing, or Parquet reader changes
- live data, AI/context collectors, or live trading

Next PR:

```text
PR 11 - QuestDB Health Check / Ledger Foundation
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
.\.venv\Scripts\python.exe scripts/check_cost_model.py
.\.venv\Scripts\python.exe scripts/check_label_builder.py
.\.venv\Scripts\python.exe -m pytest
powershell -ExecutionPolicy Bypass -File scripts/run_tests.ps1
```

---

## PR 8 Rule

Formal parity assertions compare historical and live outputs only when both
paths receive equivalent event-time-ordered inputs. The historical helper sorts
by `event_time`; the live helper processes caller order and does not reject
out-of-order records.

Out-of-order live arrival is supported by PR 7's `FeatureBuilder`, but it is not
the main parity condition. Same-timestamp ordering matters because equal
timestamps do not define a unique order by themselves. This preserves the PR 7
batch sorting vs live arrival order distinction.

---

## Files To Know

Core contracts:

```text
src/market_relay_engine/contracts/
```

Feature builder:

```text
src/market_relay_engine/market_data/feature_builder.py
src/market_relay_engine/market_data/cost_model.py
src/market_relay_engine/market_data/label_builder.py
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
scripts/check_cost_model.py
scripts/check_label_builder.py
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
docs/cost_model.md
docs/label_builder.md
docs/configuration.md
```

---

## Next Steps

1. Review PR 10 on GitHub after it is opened.
2. Check out or pull branch `pr10-label-builder-for-supervised-model` on the
   server laptop.
3. Run the full validation commands from the Standard Server-Laptop Validation
   section.
4. Merge PR 10 if review and server-laptop validation are clean.
5. Start PR 11 - QuestDB Health Check / Ledger Foundation.
