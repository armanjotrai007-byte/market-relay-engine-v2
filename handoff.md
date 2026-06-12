# handoff.md - Trading System V2 Clean Handoff

## Current Status

Repository: `armanjotrai007-byte/market-relay-engine-v2`

Canonical source of truth: GitHub `main`.

Current active PR:

- **PR 22 - Fill / Position Reconciliation**
- Branch: `pr22-fill-position-reconciliation`
- Purpose: convert execution-level fill payloads to `FillEvent`, apply fills to
  `PortfolioState`, calculate slippage from submit-time expected price, reconcile
  local vs broker quantity, and build reconciliation health events.
- Safety exclusions: no Alpaca calls, no order submission, no QuestDB writes, no
  live trading, no retries, no async service, no model inference, no AI calls, no
  external collectors, and no new heavy dependencies.
- Next PR after merge: **PR 23 - Fake/Paper End-to-End Loop**

Latest confirmed merged base before PR22:

- **PR 21 merge commit:** `18c11be8dd54a2aee42a7c340239d6a7e95eef6d`

Local workspace and publishing note:

- This local workspace is not a usable Git checkout for publishing work.
- Branches and PRs may need to be created on GitHub with the GitHub connector
  when this workspace remains connector-only.

---

## Project Summary

This repo builds a local AI-assisted trading research and paper/live execution
system.

Core flow:

```text
Databento market data
-> normalized MarketRecord
-> canonical feature builder
-> model signal
-> deterministic risk filter
-> local order intent
-> future Alpaca paper/live execution
-> QuestDB bot ledger
```

QuestDB is only the bot ledger. It must not be used as a historical market-data
warehouse.

Historical market truth comes from official Databento historical DBN/Parquet
files, not QuestDB.

---

## Non-Negotiable Rules

1. GitHub is the official project filesystem.
2. Test every PR on the server laptop before merging.
3. Keep raw Databento files local and ignored.
4. Do not commit `.dbn`, `.dbn.zst`, `.parquet`, logs, `.env`, or API keys.
5. Do not use QuestDB as historical market-data storage.
6. Use one canonical feature builder for historical and live paths.
7. AI context may produce structured risk flags only; it must not directly trade.
8. The deterministic Python risk filter is the final gate before local order
   intent creation.
9. Alpaca starts as paper trading only; live trading remains out of scope.
10. Keep PRs small, simple, testable, and reviewable.

---

## Compatibility Notes

PR8 feature parity note: historical batch sorting vs live arrival order must
remain documented because historical replay sorts by event_time while live
processing preserves arrival order.

PR19 owns local position accounting and duplicate fill protection through
`PortfolioState.applied_fill_ids`.

PR20 submits Alpaca paper orders only when explicitly enabled.

PR21 captures order-submission results and optional submit-time
`arrival_midprice`.

---

## Current PR

### PR 22 - Fill / Position Reconciliation

Branch:

```text
pr22-fill-position-reconciliation
```

Purpose:

Add a pure local fill-processing and position-reconciliation layer. PR22 converts
execution-level Alpaca-like fill payloads into `FillEvent`, applies them to
`PortfolioState`, calculates slippage using submit-time expected price, compares
local signed quantity against broker signed quantity, and builds local
reconciliation health events for future logging or monitoring.

Key behavior:

- `FillEvent` now includes optional `slippage_bps`, `broker_fill_id`,
  `model_signal_id`, and `risk_decision_id` fields.
- Fill conversion requires a unique execution-level fill id from `execution_id`,
  `activity_id`, `id`, or `trade_id`.
- Aggregate order payloads are not treated as fills because broker order IDs can
  collapse multiple partial fills into one duplicate-protected `fill_id`.
- Fill `order_id` uses `OrderSubmissionResult.local_order_id`, then
  `client_order_id`, then `source_signal_id` for local/client order correlation.
- `OrderSubmissionResult.source_signal_id` maps to `FillEvent.model_signal_id`.
- `broker_order_id` is not added to `FillEvent` or `fill_events` rows.
- Slippage uses explicit expected price, then `arrival_midprice`, then remains
  unavailable.
- Positive slippage means worse execution for both BUY and SELL.
- Missing or invalid expected price leaves `expected_price`, `slippage`, and
  `slippage_bps` as `None`.
- Broker position snapshots use PR19 signed quantity convention.
- Reconciliation reports match or mismatch and never auto-corrects positions.
- A broker snapshot passed to `apply_fill_and_reconcile(...)` must be fresh
  post-fill or periodic reconciliation state; stale snapshots can produce
  expected temporary mismatches.
- `build_position_reconciliation_health_event(...)` returns a `SystemHealthEvent`
  object only and does not write to QuestDB.
- `fill_event_to_row(...)` reads fill metadata from `FillEvent` attributes by
  default and still allows explicit keyword overrides.

Explicitly not added:

- Alpaca calls
- order submission
- polling loops
- QuestDB writes
- live trading
- retries
- async/background services
- model inference
- model training
- AI calls
- external context collectors
- new heavy dependencies

---

## Standard Server-Laptop Validation

Run from the repo root after checking out the PR branch:

```powershell
.\.venv\Scripts\python.exe scripts/check_environment.py
.\.venv\Scripts\python.exe scripts/check_config.py
.\.venv\Scripts\python.exe scripts/check_questdb.py
.\.venv\Scripts\python.exe scripts/check_questdb_schema.py
.\.venv\Scripts\python.exe scripts/check_questdb_writer.py
.\.venv\Scripts\python.exe scripts/check_questdb_analysis.py
.\.venv\Scripts\python.exe scripts/check_contracts.py
.\.venv\Scripts\python.exe scripts/check_fixtures.py
.\.venv\Scripts\python.exe scripts/check_historical_parquet.py
.\.venv\Scripts\python.exe scripts/check_dbn_inspector.py
.\.venv\Scripts\python.exe scripts/check_feature_builder.py
.\.venv\Scripts\python.exe scripts/check_feature_parity.py
.\.venv\Scripts\python.exe scripts/check_cost_model.py
.\.venv\Scripts\python.exe scripts/check_label_builder.py
.\.venv\Scripts\python.exe scripts/check_risk_filter.py
.\.venv\Scripts\python.exe scripts/check_risk_logging.py
.\.venv\Scripts\python.exe scripts/check_order_manager.py
.\.venv\Scripts\python.exe scripts/check_position_state.py
.\.venv\Scripts\python.exe scripts/check_alpaca_paper.py
.\.venv\Scripts\python.exe scripts/check_execution_metrics.py
.\.venv\Scripts\python.exe scripts/check_fill_reconciliation.py
.\.venv\Scripts\python.exe -m pytest
powershell -ExecutionPolicy Bypass -File scripts/run_tests.ps1
```

Optional real Alpaca paper account validation on the server laptop:

```powershell
.\.venv\Scripts\python.exe scripts/check_alpaca_paper.py --required
```

The required Alpaca check must only call `GET /v2/account`. It must not submit a
paper order.

With QuestDB running on the server laptop, also run:

```powershell
.\.venv\Scripts\python.exe scripts/check_questdb.py --required
.\.venv\Scripts\python.exe scripts/check_questdb_schema.py --apply --required
.\.venv\Scripts\python.exe scripts/check_questdb_writer.py --required
.\.venv\Scripts\python.exe scripts/check_questdb_analysis.py --required
```

---

## Files To Know

Execution:

```text
src/market_relay_engine/execution/order_manager.py
src/market_relay_engine/execution/position_state.py
src/market_relay_engine/execution/alpaca_paper.py
src/market_relay_engine/execution/execution_metrics.py
src/market_relay_engine/execution/fill_reconciliation.py
docs/order_manager.md
docs/position_state.md
docs/alpaca_paper.md
docs/execution_metrics.md
docs/fill_reconciliation.md
scripts/check_order_manager.py
scripts/check_position_state.py
scripts/check_alpaca_paper.py
scripts/check_execution_metrics.py
scripts/check_fill_reconciliation.py
tests/unit/test_order_manager.py
tests/unit/test_position_state.py
tests/unit/test_alpaca_paper.py
tests/unit/test_execution_metrics.py
tests/unit/test_fill_reconciliation.py
```

Core contracts:

```text
src/market_relay_engine/contracts/
```

QuestDB:

```text
src/market_relay_engine/questdb/health.py
src/market_relay_engine/questdb/writer.py
src/market_relay_engine/questdb/analysis.py
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
scripts/check_risk_filter.py
scripts/check_risk_logging.py
scripts/check_order_manager.py
scripts/check_position_state.py
scripts/check_alpaca_paper.py
scripts/check_execution_metrics.py
scripts/check_fill_reconciliation.py
scripts/check_questdb.py
scripts/check_questdb_analysis.py
scripts/run_tests.ps1
```

---

## Next Steps

1. Review PR 22 on GitHub after it is opened.
2. Check out or pull branch `pr22-fill-position-reconciliation` on the server
   laptop.
3. Run the full validation commands from the Standard Server-Laptop Validation
   section.
4. Merge PR 22 if review and server-laptop validation are clean.
5. Start PR 23 - Fake/Paper End-to-End Loop.
