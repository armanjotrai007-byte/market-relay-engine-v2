# handoff.md - Trading System V2 Clean Handoff

## Current Status

Repository: `armanjotrai007-byte/market-relay-engine-v2`

Canonical source of truth: GitHub `main`.

Current active PR:

- **PR 20 - Alpaca Paper Client Wrapper**
- Branch: `pr20-alpaca-paper-client-wrapper`
- Purpose: add a lightweight paper-only Alpaca wrapper for PR19 resolved order
  intents.
- Safety exclusions: no live trading, no live endpoint, no real order submission
  in tests/check scripts, no automatic retries, no bracket orders, no stop loss,
  no take profit, no limit order support, no options, no crypto, no QuestDB
  writes, no model inference, no AI calls, no external collectors, no async
  services, and no new heavy dependencies.
- Next PR after merge: **PR 21 - Execution Metrics / Order Result Capture**

Latest confirmed merged base before PR20:

- **PR 19 merge commit:** `7f1f9d2deaac437b226604ed15dda2f54a1b3b27`

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
9. Alpaca starts as paper trading only; PR20 must not support live trading.
10. Keep PRs small, simple, testable, and reviewable.

---

## Compatibility Notes

PR8 feature parity note: historical batch sorting vs live arrival order must
remain documented because historical replay sorts by event_time while live
processing preserves arrival order.

---

## Current PR

### PR 20 - Alpaca Paper Client Wrapper

Branch:

```text
pr20-alpaca-paper-client-wrapper
```

Purpose:

Add the first lightweight Alpaca paper-trading client wrapper. PR20 takes a PR19
`ResolvedOrderIntent` and submits it to Alpaca paper trading only when explicitly
enabled.

Key behavior:

- Paper-only base URL is exactly `https://paper-api.alpaca.markets` after
  trimming trailing slashes.
- Live URLs, lookalike paper hostnames, URL paths, and non-HTTPS paper URLs are
  rejected.
- `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` are required only when paper trading
  is enabled.
- Secrets are hidden from reprs and redacted from broker/network error messages.
- `get_account()` calls `GET /v2/account`.
- `submit_order(...)` calls `POST /v2/orders`.
- The client uses Alpaca `APCA-API-KEY-ID` and `APCA-API-SECRET-KEY` headers,
  not bearer authorization.
- `submit_order(...)` accepts only resolved BUY/SELL intents.
- PR18 `CLOSE_POSITION` intents must go through PR19
  `resolve_close_position_intent(...)` before submission.
- `CLOSE_POSITION` is rejected with local safety guidance.
- Every order payload includes deterministic `client_order_id` for idempotency.
- `client_order_id` prefers `intent.order_id`, then `intent.source_signal_id`,
  is sanitized, and is kept at 128 characters or less with a stable hash suffix
  for longer local IDs.
- Quantity is formatted as a safe string with at most 9 decimal places and no
  scientific notation.
- PR20 supports MARKET orders only; limit-style intents are rejected.
- Broker/network failures return `AlpacaPaperResponse(success=False, ...)`.
- Local safety/config failures raise `AlpacaPaperError`.
- Default check script mode uses a fake HTTP client and makes no network calls.
- Required check mode only calls account connectivity and never submits an order.

Explicitly not added:

- live trading
- live endpoint
- real order submission in tests/check scripts
- automatic retries
- bracket orders
- stop loss
- take profit
- limit order support
- options
- crypto
- QuestDB writes
- model inference
- model training
- AI calls
- external context collectors
- async/background services
- new heavy dependencies

Next PR:

```text
PR 21 - Execution Metrics / Order Result Capture
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
.\.venv\Scripts\python.exe scripts/check_alpaca_paper.py
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
docs/order_manager.md
docs/position_state.md
docs/alpaca_paper.md
scripts/check_order_manager.py
scripts/check_position_state.py
scripts/check_alpaca_paper.py
tests/unit/test_order_manager.py
tests/unit/test_position_state.py
tests/unit/test_alpaca_paper.py
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
scripts/check_questdb.py
scripts/check_questdb_analysis.py
scripts/run_tests.ps1
```

---

## Next Steps

1. Review PR 20 on GitHub after it is opened.
2. Check out or pull branch `pr20-alpaca-paper-client-wrapper` on the server
   laptop.
3. Run the full validation commands from the Standard Server-Laptop Validation
   section.
4. Optionally run account-only Alpaca paper validation with local server-laptop
   keys.
5. Run required QuestDB checks with QuestDB running.
6. Merge PR 20 if review and server-laptop validation are clean.
7. Start PR 21 - Execution Metrics / Order Result Capture.
