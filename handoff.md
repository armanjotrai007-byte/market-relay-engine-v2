# handoff.md — Trading System V2 Clean Handoff

## Status

This file is the current source of truth for `market-relay-engine-v2`.

It resolves the PR-order desync between the older PR plan PDF and the newer handoff/build plan.

## Decision on PR Order Desync

Use the updated V2 build sequence below, not the older PR plan PDF order.

The older PDF listed PR 3 as logging/timestamps, while the handoff listed PR 3 as core contracts. The correct decision is:

```text
PR 3 = Core contracts + timestamp/logging standards
```

Reason:

- Timestamp and logging requirements should not be a standalone disconnected layer.
- Every future record type needs timestamp fields, source fields, version fields, and stable serialization.
- Core contracts must define these standards before feature building, risk decisions, QuestDB ledger writes, execution metrics, and weekly analysis are built.
- PR 1 already added basic skeleton/local validation.
- PR 2 already organized V2 configs and config validation.
- Therefore, the next useful foundation is typed data contracts with standardized timestamp/log metadata.

The old “Add logging timestamps” work is not discarded. It is absorbed into PR 3.

---

## Project Summary

This is a local AI-assisted trading research and paper/live execution system.

Databento live DBN market data will feed a canonical feature builder and neural-network signal model. Structured context sources and AI-interpreted unstructured context will feed a deterministic Python risk gate. Alpaca will execute approved trades, starting with paper trading. QuestDB will act only as the bot ledger / black-box recorder for model signals, risk decisions, context flags, orders, fills, latency, slippage, PnL, outcomes, and system health.

QuestDB must not be used as a historical Databento market-data warehouse.

---

## Official Repository Workflow

GitHub is the official project filesystem and source of truth.

A local clone may be used for development and testing, but the actual trading laptop is separate. Anything needed on the trading laptop must be committed to GitHub, pulled onto that laptop, and validated locally through PowerShell commands, pytest, and committed health-check scripts.

Do not rely on hidden local files, uncommitted setup, manual-only configuration, or machine-specific assumptions.

---

## Completed / Current PR Progress

### PR 1 — Clean Skeleton

Status: implemented and opened as draft PR.

PR:

```text
https://github.com/armanjotrai007-byte/market-relay-engine-v2/pull/1
```

Branch:

```text
pr1-clean-skeleton
```

Commit:

```text
c29d3d366114458959c71de4c6a6db512383950f
```

Commit message:

```text
Add clean project skeleton and local validation scripts
```

What PR 1 added:

- clean repo skeleton
- config/docs placeholders
- Python package layout
- tests
- `.env.example`
- `.gitignore`
- PowerShell validation scripts
- local health check script
- no external API calls
- no broker logic
- no live trading
- no model training
- no RL
- no QuestDB market-data ingestion

Validation from Codex:

```text
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe scripts/check_environment.py     PASS
.\.venv\Scripts\python.exe -m pytest                        8 passed
powershell -ExecutionPolicy Bypass -File scripts/run_tests.ps1 PASS
```

Note:

Codex used the GitHub connector to create the branch, commit, and PR because local `git`/`gh` were not installed in its PowerShell environment.

---

### PR 2 — Config Organization and Validation

Status: implemented and opened as draft PR.

PR:

```text
https://github.com/armanjotrai007-byte/market-relay-engine-v2/pull/2
```

Branch:

```text
pr2-config-organization
```

Commit:

```text
08f93d07572376dfa95f5ec9367b70a9d5286ccc
```

Commit message:

```text
Organize V2 config files and validation checks
```

What PR 2 added:

- organized all seven V2 config files
- added `calendar_events.yaml`
- added `execution.yaml`
- added config loader helpers
- added `scripts/check_config.py`
- updated `scripts/check_environment.py`
- updated `scripts/run_tests.ps1`
- added `docs/configuration.md`
- added config-focused unit tests
- kept all external services disabled or development-safe by default
- added no APIs
- added no broker logic
- added no live trading
- added no QuestDB writes/schemas
- added no model code
- added no RL
- added no notebook logic

Validation from Codex:

```text
python scripts/check_environment.py                          PASS
python scripts/check_config.py                               PASS
python -m pytest                                             19 passed
powershell -ExecutionPolicy Bypass -File scripts/run_tests.ps1 PASS
```

Important note:

Codex reported that it preserved current `main` state where `AGENT.md` had been deleted. Going forward, treat `AGENTS.md` as the main coding-agent instruction file. `AGENT.md` may exist as an optional alias, but `AGENTS.md` should be the canonical agent instruction file.

---

## Current Canonical Build Order

This is the official build order from this point forward.

### PR 1 — Clean Skeleton

Completed / PR opened.

Purpose:

- repo skeleton
- docs placeholders
- package layout
- `.env.example`
- `.gitignore`
- basic tests
- health-check script
- PowerShell validation script

No external integrations.

---

### PR 2 — Config Organization and Validation

Completed / PR opened.

Purpose:

- V2 config files
- config loader
- config validation script
- config-focused tests
- documentation for configuration

Expected config files:

```text
config/symbols.yaml
config/context_sources.yaml
config/risk_limits.yaml
config/questdb.yaml
config/model_config.yaml
config/calendar_events.yaml
config/execution.yaml
```

No external integrations.

---

### PR 3 — Core Contracts + Timestamp/Logging Standards

This is the next PR.

Purpose:

Define the internal data contracts that every later system component will use.

This PR should include timestamp/logging standards because every contract must carry clean timing and traceability fields.

Do not build feature calculations, QuestDB writes, Alpaca execution, Databento connections, risk logic, model inference, or external API collectors in PR 3.

#### PR 3 should create typed contracts for:

```text
MarketRecord
FeatureSnapshot
ModelSignal
RiskDecision
ContextIndicatorSnapshot
ContextAIEvent
ContextFlag
OrderEvent
FillEvent
TradeOutcome
LatencyMetric
SystemHealthEvent
```

#### PR 3 should define common metadata fields:

Every event-like contract should include the relevant subset of:

```text
event_time
source_event_time
local_receive_time
decision_time
write_time
ticker
symbol
source
schema_version
feature_version
model_version
risk_version
calibration_version
run_id
session_id
trace_id
```

#### PR 3 should define timestamp utilities beyond PR 1 basics:

```text
utc_now()
to_utc_iso()
parse_utc_iso()
ensure_timezone_aware_utc()
monotonic_time_ms()
```

#### PR 3 should define logging/trace standards:

- standard logger factory
- run/session ID generation
- trace ID generation
- structured log context helper
- no real log ingestion
- no QuestDB writes
- no external services

#### PR 3 should include tests that prove:

- all contracts serialize to JSON
- all contracts include required timestamp/version fields where appropriate
- UTC timestamps are timezone-aware
- trace IDs/session IDs are non-empty
- example records can round-trip through dict/JSON
- no internet or external service is needed
- no secrets are required

#### Suggested PR 3 branch:

```text
pr3-core-contracts-and-time-standards
```

#### Suggested PR 3 commit message:

```text
Add core contracts and timestamp standards
```

---

### PR 4 — Canonical Feature Builder Skeleton

Purpose:

Create the first version of the canonical feature builder used by both historical and live paths.

Do not build full ML training yet.

Must establish:

- one feature builder path
- feature schema version
- deterministic feature snapshot output
- fake/sample market record inputs
- no notebook-only feature logic

Key rule:

```text
No feature is allowed into the model unless it is generated by the same code path used for both historical and live execution.
```

---

### PR 5 — Historical/Live Feature Parity Tests

Purpose:

Prove that historical replay and live-style processing produce identical feature snapshots for equivalent input records.

This PR exists to prevent train-serve skew.

---

### PR 6 — Cost Model V1

Purpose:

Add realistic trading cost assumptions before model training.

Should include:

- spread cost
- estimated slippage
- missed-fill assumptions
- minimum expected edge
- net return after costs
- conservative counterfactual fill assumptions for blocked trades

No model training yet.

---

### PR 7 — QuestDB Bot Ledger Contracts

Purpose:

Create QuestDB ledger contract definitions and schema planning for bot events only.

Allowed table concepts:

```text
model_signals
risk_decisions
context_indicator_snapshots
context_ai_events
orders
fills
trade_outcomes
latency_metrics
system_health
```

Forbidden V1/raw market-data tables:

```text
raw_trades
raw_bbo
raw_tbbo
raw_ohlcv
historical Databento warehouse tables
```

No historical Databento market-data warehouse behavior.

---

### PR 8 — QuestDB Health Check + Ledger Writer Stub

Purpose:

Add connection health check and fake/test ledger write helpers.

Do not write real market data.

Do not create raw market tables.

---

### PR 9 — Tiny JSONL Ledger Fallback

Purpose:

If QuestDB ledger write fails, append event JSON to:

```text
data/emergency_ledger/YYYYMMDD.jsonl
```

This is only for bot ledger events.

Do not rebuild a large spool/replay system.

---

### PR 10 — Alpaca Paper Execution Metrics Stub

Purpose:

Add paper-execution wrapper structure and execution metric records.

Required metrics:

```text
order_sent_time
broker_ack_time
fill_time
arrival_midprice
expected_price
fill_price
slippage
time_to_fill_ms
missed_fill_status
```

No live trading.

---

### PR 11 — Risk Filter V1

Purpose:

Add deterministic approve/block/reduce logic.

Inputs:

- model signal
- confidence
- spread
- latency
- context flags
- event windows
- portfolio state
- account/day state

Outputs:

```text
APPROVE
BLOCK
REDUCE_SIZE
EXIT
DO_NOTHING
```

The AI context filter must not directly approve trades.

---

### PR 12 — ContextState Cache

Purpose:

Create in-memory latest-context cache.

Risk reads from memory, not from QuestDB.

---

### PR 13 — Structured Collectors V1

Purpose:

Add structured collectors gradually.

Start with:

1. sector/index proxy collector
2. EIA release-window collector
3. FRED yields/rates collector
4. USAspending awards collector
5. calendar events collector

Each collector must:

- update ContextState
- log snapshots later through ledger layer
- avoid per-tick API calls
- handle stale data safely

---

### PR 14 — AI Context Filter V1

Purpose:

Convert text into structured risk flags.

Inputs:

- SEC filing text
- news text
- social/political post text
- contract descriptions

Outputs strict JSON schema only.

AI cannot trade directly.

---

### PR 15 — SEC/News/Social Input Stubs

Purpose:

Add simple input paths for unstructured text.

Start with SEC EDGAR metadata/text for watched tickers and simple news/social stubs.

Do not overbuild.

---

### PR 16 — Model Inference Interface

Purpose:

Define:

```python
predict(features) -> ModelSignal
```

Use a fake model first.

No real training yet.

---

### PR 17 — Supervised Signal Model

Purpose:

Train first supervised model using:

- official Databento historical Parquets
- canonical feature builder
- cost-aware labels
- walk-forward validation

Do not use QuestDB historical market data for model training.

---

### PR 18 — Confidence Calibration

Purpose:

Add:

- reliability chart data
- calibration method
- calibration version
- threshold validation

Do not trust raw model confidence for live thresholds until calibrated.

---

### PR 19 — End-to-End Paper Loop

Purpose:

Connect:

- Databento live or mocked market records
- feature builder
- model inference
- ContextState
- risk filter
- Alpaca paper execution
- QuestDB ledger

Paper only.

---

### PR 20 — Weekly Analysis

Purpose:

Join:

```text
QuestDB bot/context ledger exports
+
official Databento market Parquets
+
historical context Parquets where available
```

Analyze:

- approved trades
- blocked signals
- estimated blocked-trade outcomes
- slippage
- latency
- filter usefulness
- context usefulness
- recommendations

Weekly reports should recommend changes, not auto-deploy them.

---

### PR 21 — Regime and Portfolio Controls

Purpose:

Add:

- feature drift monitoring
- volatility/spread/volume drift monitoring
- confidence-distribution monitoring
- sector exposure limits
- correlated-name limits
- gross/net exposure limits
- drawdown/slippage/loss circuit breakers

---

### PR 22+ — Later RL Only

RL comes only after:

- supervised baseline works
- cost model is realistic
- paper trading loop works
- simulator is realistic
- weekly analysis shows enough signal quality

RL should focus on:

- exit timing
- hold/reduce/exit
- position sizing
- trade management

RL should not be treated as a magic market predictor.

---

## Non-Negotiable Architecture Rules

1. Do not chase microsecond/sub-second alpha through Alpaca.
2. Target practical horizons such as 30 seconds to 5 minutes or longer.
3. Use Databento live DBN as the core live market feed.
4. Use official Databento historical Parquets or DBN-to-Parquet as market truth for training/backtesting.
5. Use one canonical feature builder for historical, backtest, paper, and live paths.
6. Do not use QuestDB as a historical market-data warehouse.
7. Use QuestDB only as the bot ledger / black-box recorder.
8. Do not query QuestDB in the per-tick decision loop.
9. Use in-memory state for live context reads.
10. Use a deterministic Python risk script as the final trade gate.
11. AI context filter outputs structured flags only.
12. AI must not directly approve or place trades.
13. Start with Alpaca paper trading only.
14. Log every model signal, including blocked signals.
15. Measure Alpaca execution quality from day one.
16. Use cost-aware labels.
17. Calibrate model confidence before using it as a serious threshold.
18. Do not aggressively tune filters from one week of data.
19. Use yfinance only for development/secondary context unless replaced by a production-quality source.
20. Keep PRs small, testable, and reviewable.

---

## What Is Being Traded

Initial universe:

- selected liquid oil-related equities
- selected liquid defense-related equities

Example starting names:

```text
Oil/Energy: XOM, CVX
Defense: LMT, RTX, NOC, GD
```

Final tickers must live in:

```text
config/symbols.yaml
```

Context symbols are not automatically tradable symbols.

---

## Data Sources

### Core Market Data

- Databento live DBN for live features.
- Databento historical Parquets or DBN-to-Parquet for training/backtesting.

### Execution

- Alpaca, starting with paper trading.

### Structured Context

- EIA
- FRED
- USAspending
- sector/index/oil proxies
- calendar events
- yfinance only as development/secondary context unless replaced

### Unstructured Context

- SEC EDGAR
- news
- social/political posts
- contract descriptions
- policy/geopolitical headlines

Unstructured context must go through the AI context filter and become structured risk flags.

---

## Testing Standard for Every PR

Every PR should be tested locally and on the server/trading laptop clone using PowerShell.

Expected baseline commands:

```powershell
python scripts/check_environment.py
python scripts/check_config.py
python -m pytest
powershell -ExecutionPolicy Bypass -File scripts/run_tests.ps1
```

If a PR adds a new health check, add it to:

```text
scripts/run_tests.ps1
```

Every PR description should include:

1. Summary of changes.
2. What was intentionally not included.
3. Confirmation no unwanted external APIs/trading behavior were added.
4. Test commands run.
5. Test results.
6. Next recommended PR.
