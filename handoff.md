# handoff.md - Trading System V2 Clean Handoff

## Current Status

Repository: `armanjotrai007-byte/market-relay-engine-v2`

Canonical source of truth: GitHub `main`.

Current active PR:

- **PR 21 - Execution Metrics / Order Result Capture**
- Branch: `pr21-execution-metrics-order-result-capture`
- Purpose: capture local order-submission results from PR19 resolved intents,
  PR20 Alpaca paper responses, caller-provided UTC timestamps, and optional
  submit-time arrival midprice.
- Safety exclusions: no Alpaca calls, no order submission, no QuestDB writes, no
  fills, no reconciliation, no retries, no live trading, no model inference, no
  AI calls, no external collectors, and no new heavy dependencies.
- Next PR after merge: fill/reconciliation work can combine captured
  `arrival_midprice` with future fill prices.

Latest confirmed merged base before PR21:

- **PR 20 merge commit:** `0f99464c425a5180862ab09485fb49f6e7de8a49`

Local workspace and publishing note:

- This local workspace is not a usable Git checkout: `.git` is absent, and
  `git` / `gh` were not available on PATH during recent publishing work.
- Branches may need to be created on GitHub with the GitHub connector when this
  workspace remains connector-only.

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

---

## Current PR

### PR 21 - Execution Metrics / Order Result Capture

Branch:

```text
pr21-execution-metrics-order-result-capture
```

Purpose:

Add a lightweight capture-only layer that takes a PR19 `ResolvedOrderIntent`, a
PR20 `AlpacaPaperResponse`, caller-provided UTC timestamps, and optional
submit-time `arrival_midprice`, then returns structured local execution records
for future ledger writing.

Key behavior:

- `OrderSubmissionResult` links local intent IDs, client order IDs, broker order
  IDs, status, error text, timing, and optional arrival midprice.
- Latency uses only local caller timestamps:
  `(submit_completed_at - submit_started_at).total_seconds() * 1000.0`.
- `latency_ms` must be finite and greater than or equal to `0.0`; zero latency
  is valid for mocked or fail-fast local paths.
- Broker timestamps are not used for latency because local and broker clocks can
  drift. Future broker timestamps are audit metadata only.
- PR21 receives timestamps from callers. Future timestamp-capturing wrappers
  should use `market_relay_engine.common.time` UTC helpers.
- `capture_order_submission_result(...)` expects an already-resolved BUY/SELL
  intent. Unresolved `CLOSE_POSITION` is not supported by PR21 capture.
- Client order ID fallback is explicit argument, safe raw response
  `client_order_id`, `local_order_id`, `intent.order_id`, then
  `source_signal_id`.
- All raw response access is defensive and `raw_response` is never stored in
  `OrderSubmissionResult`.
- `order_type` and `time_in_force` prefer intent metadata, then safe raw response
  metadata, then PR20 market/day defaults.
- `arrival_midprice` is preserved on `OrderSubmissionResult` and maps to
  future `order_events.expected_price`.
- `build_order_event_payload(...)` emits only existing `order_events` schema
  keys and does not include `arrival_midprice`, `client_order_id`, `status_code`,
  `error_message`, `submit_started_at`, or `submit_completed_at`.
- `build_latency_metric_payload(...)` uses
  `alpaca_order_submit_latency_ms` as the central metric name.
- Default check script mode is offline and makes no network calls.

Explicitly not added:

- Alpaca calls
- order submission
- QuestDB writes
- fills
- reconciliation
- retries
- live trading
- model inference
- model training
- AI calls
- external context collectors
- async/background services
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

Risk:

```text
src/market_relay_engine/risk/
docs/risk_filter.md
docs/risk_logging.md
scripts/check_risk_filter.py
scripts/check_risk_logging.py
tests/unit/test_risk_filter.py
tests/unit/test_risk_logging.py
```

Execution:

```text
src/market_relay_engine/execution/order_manager.py
src/market_relay_engine/execution/position_state.py
src/market_relay_engine/execution/alpaca_paper.py
src/market_relay_engine/execution/execution_metrics.py
docs/order_manager.md
docs/position_state.md
docs/alpaca_paper.md
docs/execution_metrics.md
scripts/check_order_manager.py
scripts/check_position_state.py
scripts/check_alpaca_paper.py
scripts/check_execution_metrics.py
tests/unit/test_order_manager.py
tests/unit/test_position_state.py
tests/unit/test_alpaca_paper.py
tests/unit/test_execution_metrics.py
```

Core contracts:

```text
src/market_relay_engine/contracts/
```

Feature/cost/label builders:

```text
src/market_relay_engine/market_data/feature_builder.py
src/market_relay_engine/market_data/cost_model.py
src/market_relay_engine/market_data/label_builder.py
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
scripts/check_questdb.py
scripts/check_questdb_analysis.py
scripts/run_tests.ps1
```

---

## Next Steps

1. Review PR 21 on GitHub after it is opened.
2. Check out or pull branch `pr21-execution-metrics-order-result-capture` on the
   server laptop.
3. Run the full validation commands from the Standard Server-Laptop Validation
   section.
4. Optionally run account-only Alpaca paper validation with local server-laptop
   keys.
5. Run required QuestDB checks with QuestDB running.
6. Merge PR 21 if review and server-laptop validation are clean.
