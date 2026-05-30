# handoff.md - Trading System V2 Clean Handoff

## Current Status

Repository: `armanjotrai007-byte/market-relay-engine-v2`

Canonical source of truth: GitHub `main`.

Current active PR:

- **PR 18 - Order Manager V1**
- Branch: `pr18-order-manager-v1`
- Purpose: create a lightweight local order-intent safety layer after PR16 risk
  decisions and PR17 risk logging.
- Safety exclusions: no Alpaca, broker submission, live trading, QuestDB
  writes, model inference, AI calls, external collectors, async services, JSONL
  fallback, retry queues, full account/position state, broker-specific rounding,
  lot-size rules, or new heavy dependencies.
- Next PR after merge: **PR 19 - Position and Account State V1**

Latest confirmed merged base before PR18:

- **PR 17 merge commit:** `690146659ce1ed50039309b1569104ff59d22520`

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
9. Alpaca starts as paper trading only and is not added in PR18.
10. Keep PRs small, simple, testable, and reviewable.

---

## Compatibility Notes

PR8 feature parity note: historical batch sorting vs live arrival order must
remain documented because historical replay sorts by event_time while live
processing preserves arrival order.

---

## Current PR

### PR 18 - Order Manager V1

Branch:

```text
pr18-order-manager-v1
```

Purpose:

Turn an existing `ModelSignal`, a PR16 `RiskDecision`, PR17 risk log success
state, desired quantity, and in-memory order state into a safe local
`OrderManagerResult`.

Key behavior:

- `build_order_intent(...)` is pure and does not mutate state.
- `OrderIntent` is a local intent, not a broker order.
- `OrderIntentSide.CLOSE_POSITION` is a local intent side, not a broker side.
- Entry orders require successful risk logging by default.
- `EXIT` / `CLOSE_POSITION` can proceed when risk logging failed if configured,
  preserving emergency exit behavior.
- `CLOSE_POSITION` uses `quantity=None` because PR18 does not have position
  state.
- `REDUCE_SIZE` preserves fractional quantities with
  `desired_quantity * reduce_size_factor`.
- Broker-specific rounding, lot-size handling, and position translation are
  deferred to later PRs.
- `reserve_order_intent(...)` marks a signal used and adds an in-memory
  placeholder for `BUY`, `SELL`, and `CLOSE_POSITION`.
- A reserved `CLOSE_POSITION` blocks new entries for that ticker until
  `release_open_order(...)` clears the placeholder.
- Releasing an open-order placeholder does not unmark the source signal ID.

Explicitly not added:

- Alpaca
- broker submission
- live trading
- QuestDB writes
- model inference
- model training
- AI calls
- external context collectors
- async/background services
- JSONL fallback
- retries/queues
- full account state
- full position state
- broker-specific rounding or lot-size rules
- new heavy dependencies

Next PR:

```text
PR 19 - Position and Account State V1
```

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
.\.venv\Scripts\python.exe -m pytest
powershell -ExecutionPolicy Bypass -File scripts/run_tests.ps1
```

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
docs/order_manager.md
scripts/check_order_manager.py
tests/unit/test_order_manager.py
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
scripts/check_questdb.py
scripts/check_questdb_analysis.py
scripts/run_tests.ps1
```

---

## Next Steps

1. Review PR 18 on GitHub after it is opened.
2. Check out or pull branch `pr18-order-manager-v1` on the server laptop.
3. Run the full validation commands from the Standard Server-Laptop Validation
   section.
4. Run required QuestDB checks with QuestDB running.
5. Merge PR 18 if review and server-laptop validation are clean.
6. Start PR 19 - Position and Account State V1.
