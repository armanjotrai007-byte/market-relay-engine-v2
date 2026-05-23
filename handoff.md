# handoff.md â€” Trading System V2 Clean Handoff

## Current Status

Repository: `armanjotrai007-byte/market-relay-engine-v2`

Canonical source of truth: GitHub `main`.

Current active PR:

- **PR 16 - Risk Filter V1**
- Branch: `pr16-risk-filter-v1`
- Purpose: deterministic final risk gate for existing model signals.
- Adds simple market, cost, context, account, and portfolio placeholder inputs.
- Next PR after merge: **PR 17 - Risk Decision Logging**

Latest confirmed merged base before PR 15:

- **PR 13 merge commit:** `8e2fab7fe04a61399d3b587f06a95d76ed0e5c1d`

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

## Current PR

### PR 16 - Risk Filter V1

Branch:

```text
pr16-risk-filter-v1
```

Purpose:

Add a deterministic Risk Filter V1 that evaluates an existing `ModelSignal`,
PR9 `CostEstimate`, simple market quality facts, generic context risk booleans,
and account/portfolio placeholder inputs into a `RiskDecision`.

Key behavior:

- BUY and SELL entry signals require a profitable cost estimate when configured.
- HOLD and DO_NOTHING return `DO_NOTHING`.
- EXIT returns `EXIT` and bypasses entry blocking checks.
- Market data staleness uses explicit `evaluation_time - market_data_time`.
- Context risk is generic only: event window, high risk, elevated risk, optional
  snapshot ID, and machine-readable reasons.
- REDUCE_SIZE is conditionally approved and requires downstream consumers to
  apply `reduce_size_factor`.
- `thresholds_used` records only the decision-relevant rule data.

Explicitly not added:

- Alpaca
- broker execution
- QuestDB writes
- live trading
- model inference
- model training
- AI calls
- external context collectors
- EIA/FRED/USAspending/SEC/yfinance logic
- order manager
- full account state
- full portfolio state
- async/background services
- new heavy dependencies

Next PR:

```text
PR 17 - Risk Decision Logging
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

Risk filter:

```text
src/market_relay_engine/risk/
docs/risk_filter.md
scripts/check_risk_filter.py
tests/unit/test_risk_filter.py
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
scripts/check_questdb.py
scripts/check_questdb_analysis.py
scripts/run_tests.ps1
```

---

## Next Steps

1. Review PR 16 on GitHub after it is opened.
2. Check out or pull branch `pr16-risk-filter-v1` on the server laptop.
3. Run the full validation commands from the Standard Server-Laptop Validation section.
4. Run required QuestDB checks with QuestDB running.
5. Merge PR 16 if review and server-laptop validation are clean.
6. Start PR 17 - Risk Decision Logging.
