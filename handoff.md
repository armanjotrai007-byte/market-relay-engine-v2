# handoff.md - Trading System V2 Clean Handoff

## Current Status

Repository: `armanjotrai007-byte/market-relay-engine-v2`

Canonical source of truth: GitHub `main`.

Current active PR:

- **PR 19 - Position and Account State V1**
- Branch: `pr19-position-account-state-v1`
- Purpose: create lightweight local position/account state from fills and
  resolve PR18 close-position intents into concrete buy/sell details.
- Safety exclusions: no Alpaca, broker submission, live trading, QuestDB
  writes, model inference, AI calls, external collectors, async services, or new
  heavy dependencies.
- Next PR after merge: **PR 20 - Alpaca Paper Client Wrapper**

Latest confirmed merged base before PR19:

- **PR 18 merge commit:** `5d8959555146fe719049570b0239eb1171f12960`

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
9. Alpaca starts as paper trading only and is not added in PR19.
10. Keep PRs small, simple, testable, and reviewable.

---

## Compatibility Notes

PR8 feature parity note: historical batch sorting vs live arrival order must
remain documented because historical replay sorts by event_time while live
processing preserves arrival order.

---

## Current PR

### PR 19 - Position and Account State V1

Branch:

```text
pr19-position-account-state-v1
```

Purpose:

Track local position and account state from `FillEvent` records, resolve PR18
`CLOSE_POSITION` intents into concrete buy/sell details, and provide minimal
account/portfolio inputs for the risk filter.

Key behavior:

- Signed quantity convention: positive is long, negative is short, zero or
  absent is flat.
- BUY fills increase signed quantity; SELL fills decrease signed quantity.
- Same-side adds update weighted average price.
- Partial closes realize PnL and keep the remaining average price unchanged.
- Full closes remove the position.
- Fills that cross zero split accounting into old-position close and new
  opposite-side open.
- Duplicate fill IDs are ignored and do not mutate quantity or PnL.
- Account state separates `total_realized_pnl`, `daily_realized_pnl`,
  `daily_loss_dollars`, and `consecutive_losses`.
- `daily_loss_dollars` is based only on `daily_realized_pnl`.
- Daily reset helpers clear daily PnL/loss without erasing total realized PnL.
- `CLOSE_POSITION` resolves to SELL for long, BUY for short, and quantity `0`
  when flat.
- Optional PR18 `OrderManagerState` can feed duplicate/conflicting order
  placeholder state into risk inputs.
- Sector exposure is local only and groups `abs(quantity) * mark_price` by
  optional local sector labels.

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
- new heavy dependencies

Next PR:

```text
PR 20 - Alpaca Paper Client Wrapper
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
.\.venv\Scripts\python.exe scripts/check_position_state.py
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
src/market_relay_engine/execution/position_state.py
docs/order_manager.md
docs/position_state.md
scripts/check_order_manager.py
scripts/check_position_state.py
tests/unit/test_order_manager.py
tests/unit/test_position_state.py
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
scripts/check_questdb.py
scripts/check_questdb_analysis.py
scripts/run_tests.ps1
```

---

## Next Steps

1. Review PR 19 on GitHub after it is opened.
2. Check out or pull branch `pr19-position-account-state-v1` on the server laptop.
3. Run the full validation commands from the Standard Server-Laptop Validation
   section.
4. Run required QuestDB checks with QuestDB running.
5. Merge PR 19 if review and server-laptop validation are clean.
6. Start PR 20 - Alpaca Paper Client Wrapper.
