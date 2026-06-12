# handoff.md - Trading System V2 Clean Handoff

## Current Status

Repository: `armanjotrai007-byte/market-relay-engine-v2`

Canonical source of truth: GitHub `main`.

Current active PR:

- **PR 23 - Fake/Paper End-to-End Loop**
- Branch: `pr23-fake-paper-end-to-end-loop`
- Purpose: prove the local execution pipeline can go from a fake approved trade to order submission result, fill conversion, portfolio update, and reconciliation without real APIs.
- Safety exclusions: no Alpaca calls, no order submission, no QuestDB writes, no live trading, no retries, no async service, no scheduler, no model inference, no AI calls, no external collectors, and no new heavy dependencies.
- Next PR after merge: guarded real Alpaca paper smoke test.

Latest confirmed merged base before PR23:

- **PR 22 merge commit:** `48836b2a33c5404c48d3a06ee01c1dba54f26670`

Local workspace and publishing note:

- This local workspace is not a usable Git checkout for publishing work.
- Branches and PRs may need to be created on GitHub with the GitHub connector when this workspace remains connector-only.

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
-> local order intent
-> future Alpaca paper/live execution
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
8. The deterministic Python risk filter is the final gate before local order intent creation.
9. Alpaca starts as paper trading only; live trading remains out of scope.
10. Keep PRs small, simple, testable, and reviewable.

---

## Compatibility Notes

PR8 feature parity note: historical batch sorting vs live arrival order must remain documented because historical replay sorts by event_time while live processing preserves arrival order.

PR19 owns local position accounting and duplicate fill protection through `PortfolioState.applied_fill_ids`.

PR20 submits Alpaca paper orders only when explicitly enabled.

PR21 captures order-submission results and optional submit-time `arrival_midprice`.

PR22 converts execution-level fill payloads into `FillEvent`, applies them to `PortfolioState`, calculates slippage, and compares local signed quantity against broker signed quantity.

---

## Current PR

### PR 23 - Fake/Paper End-to-End Loop

Branch:

```text
pr23-fake-paper-end-to-end-loop
```

Purpose:

Add a deterministic local fake paper loop that connects the existing order manager, resolved intent path, mocked Alpaca paper response, execution metrics capture, fill reconciliation, and position state helpers.

Key behavior:

- Builds a fake `ModelSignal` and approved `RiskDecision`.
- Uses `build_order_intent(...)` to create an entry order intent.
- Customizes the frozen `OrderIntent` with `dataclasses.replace(...)`.
- Reserves the intent through `reserve_order_intent(...)` before mocked submission.
- Resolves BUY/SELL entries through `resolve_close_position_intent(...)`.
- Captures a mocked `AlpacaPaperResponse` with `capture_order_submission_result(...)`.
- Converts a manually built execution-level fill payload with `fill_event_from_alpaca_fill_payload(...)`.
- Uses fixed timezone-aware UTC timestamps for deterministic validation.
- Computes the expected broker quantity independently from starting quantity plus signed fill delta.
- Applies and reconciles through `apply_fill_and_reconcile(...)`.
- Releases the order-manager reservation and returns final `OrderManagerState` evidence.
- Does not add round-trip helper logic.

Profit-protection focus:

- clean ID propagation
- clean fill correlation
- duplicate-fill protection
- correct slippage direction
- correct local position state
- clear reconciliation mismatch detection
- no active open-order reservation left behind

Explicitly not added:

- Alpaca calls
- order submission
- polling loops
- retries
- schedulers
- async/background services
- QuestDB writes
- live trading
- Databento live feed
- model inference
- model training
- AI calls
- external context collectors
- new heavy dependencies
- strategy optimization
- backtesting engine
- profit claims

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
.\.venv\Scripts\python.exe scripts/check_fake_paper_loop.py
.\.venv\Scripts\python.exe -m pytest
powershell -ExecutionPolicy Bypass -File scripts/run_tests.ps1
```

Optional real Alpaca paper account validation on the server laptop:

```powershell
.\.venv\Scripts\python.exe scripts/check_alpaca_paper.py --required
```

The required Alpaca check must only call `GET /v2/account`. It must not submit a paper order.

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
src/market_relay_engine/execution/fake_paper_loop.py
docs/order_manager.md
docs/position_state.md
docs/alpaca_paper.md
docs/execution_metrics.md
docs/fill_reconciliation.md
docs/fake_paper_loop.md
scripts/check_order_manager.py
scripts/check_position_state.py
scripts/check_alpaca_paper.py
scripts/check_execution_metrics.py
scripts/check_fill_reconciliation.py
scripts/check_fake_paper_loop.py
tests/unit/test_order_manager.py
tests/unit/test_position_state.py
tests/unit/test_alpaca_paper.py
tests/unit/test_execution_metrics.py
tests/unit/test_fill_reconciliation.py
tests/unit/test_fake_paper_loop.py
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
scripts/check_fake_paper_loop.py
scripts/check_questdb.py
scripts/check_questdb_analysis.py
scripts/run_tests.ps1
```

---

## Next Steps

1. Review PR 23 on GitHub after it is opened.
2. Check out or pull branch `pr23-fake-paper-end-to-end-loop` on the server laptop.
3. Run the full validation commands from the Standard Server-Laptop Validation section.
4. Merge PR 23 if review and server-laptop validation are clean.
5. Start the guarded real Alpaca paper smoke test PR.
