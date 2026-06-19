# handoff.md - Trading System V2 Clean Handoff

## Current Status

Repository: `armanjotrai007-byte/market-relay-engine-v2`

Canonical source of truth: GitHub `main`.

Latest confirmed merged base:

- **PR 24 - In-Memory ContextState Cache**
- Merge commit: `942b5a99c485240f8716e0bec4785290dbdf221a`
- Result: merged into `main` with `GLOBAL`, `TICKER`, and `SECTOR` cache scopes and normal update outcomes for written, replaced, stale, and duplicate updates.

Current active PR:

- **PR 25 - YFinance Development Proxy Collector**
- Branch: `pr25-yfinance-dev-sector-proxy`
- Purpose: add a disabled-by-default, development-only, one-shot yfinance proxy collector that feeds PR24 `ContextStateCache` entries and can optionally write context indicator ledger rows.
- Safety exclusions: no trading signals, no risk approval changes, no order submission, no Alpaca calls, no Databento calls, no QuestDB reads, no scheduler, no background service, no AI/model calls, and no production market-data claims.
- Next likely PR after merge: deterministic numeric context-rule integration that consumes PR25 proxy readings for sector confirmation or other explicit rules.

Local workspace and publishing note:

- This local workspace may not be a usable Git checkout for publishing work.
- PR25 was prepared with the GitHub connector.
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

PR24 owns the in-memory context state cache. Expired entries are hidden from default reads, removed only by explicit `purge_expired(...)`, or removed indirectly by bounded capacity eviction when writes exceed `max_entries`.

---

## Current PR

### PR 25 - YFinance Development Proxy Collector

Branch:

```text
pr25-yfinance-dev-sector-proxy
```

Purpose:

Add a simple development-only proxy collector that can populate broad-market and sector context entries from yfinance for local integration testing. It remains disabled by default and does not become a production data source.

Key behavior:

- Pins `yfinance==1.4.1` and `pandas>=2.2,<3`.
- Uses one batch yfinance request with one individual fallback pass only for affected symbols.
- Supports only five-minute bars.
- Drops incomplete bars using a configurable bar-completion grace.
- Enforces `max_staleness_seconds >= 300 + bar_completion_grace_seconds`.
- Calculates only `latest_close`, `return_5m`, `return_15m`, and `return_60m`.
- Requires exact timestamp lookbacks and finite positive denominator closes for returns.
- Stores sector proxy ETF entries under existing `SECTOR` scope with names that include the proxy symbol.
- Stores XLE, XOP, and OIH under `SECTOR/OIL`, matching the configured `oil` tradable-sector label after cache normalization.
- Uses `severity=INFO` for all raw measurements.
- Adds read-only numeric retrieval helpers without QuestDB reads or risk thresholds.
- Adds deterministic `context_indicator_id` values for PR25-generated context indicator snapshots.
- Writes QuestDB rows only after cache updates return `WRITTEN` or `REPLACED`.
- Makes `--live --write-questdb` require successful QuestDB writes for produced indicators and fail on ledger write issues or zero successful rows when valid indicators exist.
- Adds `NO_FRESH_DATA` to distinguish reachable source/no fresh completed bars from source or schema failures.

Explicitly not added:

- trading rules
- risk approval changes
- external scheduler
- background service
- QuestDB reads
- new QuestDB tables
- AI or model calls
- Alpaca calls
- Databento calls
- generic collector framework

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
python scripts/check_yfinance_proxy.py
python -m pytest
powershell -ExecutionPolicy Bypass -File scripts/run_tests.ps1
```

Optional live yfinance validation:

```powershell
python scripts/check_yfinance_proxy.py --live
python scripts/check_yfinance_proxy.py --live --require-fresh
python scripts/check_yfinance_proxy.py --live --write-questdb
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

YFinance proxy:

```text
src/market_relay_engine/context/yfinance_proxy.py
docs/yfinance_proxy.md
scripts/check_yfinance_proxy.py
tests/unit/test_yfinance_proxy.py
```

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

---

## Next Steps

1. Review PR25 on GitHub after it is opened.
2. Check out or pull branch `pr25-yfinance-dev-sector-proxy` on the server laptop.
3. Run the full validation commands from the Standard Server-Laptop Validation section.
4. Optionally run the live yfinance smoke command during market hours.
5. Merge PR25 only if review and server-laptop validation are clean.
6. Start the deterministic numeric context-rule PR that consumes the PR25 retrieval helpers.
