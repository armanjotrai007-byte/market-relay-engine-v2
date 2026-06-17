# handoff.md - Trading System V2 Clean Handoff

## Current Status

Repository: `armanjotrai007-byte/market-relay-engine-v2`

Canonical source of truth: GitHub `main`.

Current active PR:

- **PR 24 - In-Memory ContextState Cache**
- Branch: `pr24-context-state-cache`
- Purpose: add a bounded, thread-safe in-memory latest context cache for global, ticker, and sector structured context facts with expiry, update statuses, JSON-safe snapshots, and `ContextStateSnapshot` aggregation.
- Safety exclusions: no external API calls, no collectors, no AI calls, no model inference, no QuestDB reads, no QuestDB writes, no order submission, no Alpaca calls, no live trading, no background services, no scheduler, no retries, and no new heavy dependencies.
- Next PR after merge: simple structured context collector/proxy feeding `ContextStateCache`.

Latest confirmed merged base before PR24:

- **PR 23 merge commit:** `ba420bba022c2df2a24065bcac9b0951c7ad80ca`

Local workspace and publishing note:

- This local workspace is not a usable Git checkout for publishing work.
- PR24 was prepared with the GitHub connector only.
- Run validation on the server laptop before merging.

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

QuestDB is only the bot ledger. It must not be used as a historical market-data warehouse or as the hot-path source for live context reads.

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

PR23 proves the local fake paper execution wiring without broker calls, QuestDB writes, model inference, AI calls, or external collectors.

---

## Current PR

### PR 24 - In-Memory ContextState Cache

Branch:

```text
pr24-context-state-cache
```

Purpose:

Add a simple in-process cache that stores the newest structured context facts in RAM for future hot-path risk reads. It prepares future risk logic to block, reduce, or warn on trades when recent context says conditions are bad, stale, expired, or high risk.

Key behavior:

- Supports `GLOBAL`, `TICKER`, and `SECTOR` context keys.
- Stores latest `ContextStateEntry` values with severity, source, `updated_at`, optional `source_event_time`, optional `valid_until`, confidence, JSON-safe details, and optional trace ID.
- Uses bounded memory with `max_entries` and deterministic oldest-entry eviction.
- Periodically purges expired entries during update attempts.
- Returns update statuses for written, replaced, stale, and duplicate updates instead of crashing on normal stale or duplicate arrival.
- Hides expired entries by default while allowing explicit `include_expired=True` reads.
- Produces JSON-safe snapshots with deep-copy isolation.
- Aggregates relevant global, sector, and ticker entries into the existing `ContextStateSnapshot` contract.
- Protects public cache methods with an internal `RLock` for basic in-process concurrent reads and writes.

Profit-protection focus:

- latest context facts can be read without external calls during future live decisions
- stale or expired context is hidden from default live snapshots
- high and critical context maps to high risk in `ContextStateSnapshot`
- medium context maps to elevated risk
- sector and ticker context can coexist with broad market context
- normal stale or duplicate collector writes do not crash the cache

Explicitly not added:

- collectors
- external API calls
- AI calls
- model inference
- QuestDB reads
- QuestDB writes
- order submission
- Alpaca calls
- live trading
- background services
- schedulers
- retries
- new heavy dependencies

---

## Standard Server-Laptop Validation

Run from the repo root after checking out the PR branch:

```powershell
python scripts/check_environment.py
python scripts/check_config.py
python scripts/check_questdb.py
python scripts/check_questdb_schema.py
python scripts/check_questdb_writer.py
python scripts/check_questdb_analysis.py
python scripts/check_contracts.py
python scripts/check_fixtures.py
python scripts/check_historical_parquet.py
python scripts/check_dbn_inspector.py
python scripts/check_feature_builder.py
python scripts/check_feature_parity.py
python scripts/check_cost_model.py
python scripts/check_label_builder.py
python scripts/check_risk_filter.py
python scripts/check_risk_logging.py
python scripts/check_order_manager.py
python scripts/check_position_state.py
python scripts/check_alpaca_paper.py
python scripts/check_execution_metrics.py
python scripts/check_fill_reconciliation.py
python scripts/check_fake_paper_loop.py
python scripts/check_context_state_cache.py
python -m pytest
powershell -ExecutionPolicy Bypass -File scripts/run_tests.ps1
```

Optional real Alpaca paper account validation on the server laptop:

```powershell
python scripts/check_alpaca_paper.py --required
```

The required Alpaca check must only call `GET /v2/account`. It must not submit a paper order.

With QuestDB running on the server laptop, also run:

```powershell
python scripts/check_questdb.py --required
python scripts/check_questdb_schema.py --apply --required
python scripts/check_questdb_writer.py --required
python scripts/check_questdb_analysis.py --required
```

---

## Files To Know

Context state cache:

```text
src/market_relay_engine/context/state_cache.py
docs/context_state_cache.md
scripts/check_context_state_cache.py
tests/unit/test_context_state_cache.py
```

Execution:

```text
src/market_relay_engine/execution/order_manager.py
src/market_relay_engine/execution/position_state.py
src/market_relay_engine/execution/alpaca_paper.py
src/market_relay_engine/execution/execution_metrics.py
src/market_relay_engine/execution/fill_reconciliation.py
src/market_relay_engine/execution/fake_paper_loop.py
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
scripts/check_context_state_cache.py
scripts/check_questdb.py
scripts/check_questdb_schema.py
scripts/check_questdb_writer.py
scripts/check_questdb_analysis.py
scripts/run_tests.ps1
```

---

## Next Steps

1. Review PR 24 on GitHub after it is opened.
2. Check out or pull branch `pr24-context-state-cache` on the server laptop.
3. Run the full validation commands from the Standard Server-Laptop Validation section.
4. Merge PR 24 if review and server-laptop validation are clean.
5. Start the simple structured context collector/proxy PR that feeds `ContextStateCache`.
