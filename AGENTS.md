# AGENT.md — Coding Agent Instructions

## Project Name

`market-relay-engine`

## Project Purpose

Build a local AI-assisted trading research and paper/live execution system.

The system uses Databento live DBN market data to build market features for a trained neural-network signal model. A deterministic Python risk filter then approves, blocks, or reduces trades using market conditions, structured context, AI-generated context flags, portfolio state, and execution limits. Alpaca is used for execution, starting with paper trading. QuestDB is used only as a live bot ledger / black-box recorder, not as a historical market-data warehouse.

## Non-Negotiable Architecture Rules 

1. Do not use QuestDB as a historical Databento market-data warehouse.
2. Historical market truth comes from official Databento historical Parquets or DBN converted to Parquet using Databento tooling.
3. Live market input comes from Databento live DBN decoded into an in-memory feature builder.
4. The neural network receives stable feature vectors, not raw DBN bytes.
5. Use one canonical `feature_builder.py` for historical training, backtesting, paper trading, and live trading.
6. Do not create separate notebook-only feature logic.
7. Do not query QuestDB inside the per-tick or per-signal decision loop.
8. Use an in-memory context cache for live context reads.
9. QuestDB records bot facts: signals, risk decisions, context flags, orders, fills, latency, slippage, PnL, outcomes, and system health.
10. AI context filtering must output structured risk flags only. It must never directly approve or place trades.
11. The final pre-trade authority is a deterministic Python risk script.
12. Start with Alpaca paper trading only. Do not enable live trading by default.
13. Do not chase microsecond or sub-second alpha through Alpaca.
14. Target realistic horizons such as 30 seconds to 5 minutes or longer.
15. Measure Alpaca execution quality from day one.
16. Do not trust model confidence until it is calibrated and validated.
17. Do not aggressively tune filters from one week of data.
18. Do not use yfinance as a production-critical feed.
19. Log every model signal, including blocked signals.
20. Keep every PR small, testable, and reviewable.

## Main System Flow

```text
Databento historical Parquets
→ canonical feature_builder.py
→ supervised training / backtesting

Databento live DBN
→ DBN decoder
→ same canonical feature_builder.py
→ calibrated signal model
→ model signal + confidence

Structured collectors + AI context filter
→ in-memory ContextState cache
→ deterministic Python risk script

Risk-approved orders
→ Alpaca paper/live execution

All signals, context, risk decisions, orders, fills, latency, slippage, and outcomes
→ QuestDB bot ledger
→ weekly Parquet export
→ filter and execution analysis
```

## What Is Being Traded

The initial universe is selected liquid oil and defense-related equities.

Examples:
- Defense: `LMT`, `RTX`, `NOC`, `GD`
- Oil/energy: `XOM`, `CVX`

The final universe must be defined in `config/symbols.yaml`.

## Data Sources

### Core Market Data

- Databento live DBN for live market feed.
- Databento historical Parquets or DBN-to-Parquet for training/backtesting.

### Execution

- Alpaca for paper/live execution.
- Paper trading must come before live trading.

### Structured Context

- EIA: oil inventory data and release windows.
- FRED: yields, rates, macro/rate regime variables.
- USAspending: defense contract awards, value, agency, recipient, ticker mapping.
- Sector/index/oil proxies: SPY, QQQ, IWM, XLE, XOP, OIH, XLI, PPA, GLD, VIX proxy, WTI/Brent/natural gas if available.
- Calendar events: CPI, FOMC, EIA releases, earnings, holidays, major scheduled events.
- yfinance may be used during development only, not as a production-critical live dependency.

### Unstructured Context

- SEC EDGAR filings.
- News headlines/articles.
- Social/political posts.
- Contract descriptions.
- Geopolitical/policy headlines.

These must pass through the AI context filter and become structured flags.

## Required Repository Shape

Use this general structure unless there is a good reason to improve it:

```text
market-relay-engine/
├─ README.md
├─ AGENT.md
├─ handoff.md
├─ pyproject.toml
├─ requirements.txt
├─ .gitignore
├─ .env.example
│
├─ docs/
│  ├─ architecture.md
│  ├─ data_contracts.md
│  ├─ weekly_analysis.md
│  ├─ risk_filter_rules.md
│  └─ live_runbook.md
│
├─ config/
│  ├─ symbols.yaml
│  ├─ context_sources.yaml
│  ├─ risk_limits.yaml
│  ├─ questdb.yaml
│  └─ model_config.yaml
│
├─ src/
│  └─ market_relay_engine/
│     ├─ __init__.py
│     ├─ common/
│     ├─ market_data/
│     ├─ context/
│     ├─ ai_context/
│     ├─ model/
│     ├─ risk/
│     ├─ execution/
│     ├─ ledger/
│     └─ analysis/
│
├─ scripts/
│  ├─ run_live_paper.py
│  ├─ check_questdb.py
│  ├─ export_weekly_ledger.py
│  ├─ run_weekly_analysis.py
│  └─ inspect_historical_parquets.py
│
├─ tests/
│  ├─ unit/
│  └─ integration/
│
├─ data/
│  ├─ raw/
│  ├─ parquet/
│  ├─ reports/
│  └─ logs/
│
└─ notebooks/
```

## Build Order

Follow this order. Do not jump ahead to live trading or RL.

### PR 1 — Repo Skeleton

Create folders, config placeholders, README, AGENT.md, handoff.md, `.env.example`, `.gitignore`, and test setup.

### PR 2 — Config, Logging, and Time Utilities

Add YAML config loading, standard logging, UTC timestamp helpers, market-time helpers, and runtime IDs.

### PR 3 — Core Data Contracts

Add typed records for:
- market records
- feature snapshots
- model signals
- risk decisions
- context flags
- orders
- fills
- trade outcomes
- system health events

All records must serialize cleanly to JSON.

### PR 4 — Canonical Feature Builder V1

Create `feature_builder.py`.

It must be used for both historical and live paths. No duplicate feature logic is allowed.

Initial features:
- bid
- ask
- midprice
- spread
- trade price
- trade size
- short-window returns
- volume
- volatility
- quote movement
- event timestamps
- local receipt timestamps
- feature version

### PR 5 — Historical/Live Feature Consistency Tests

Replay the same sample data through historical mode and live mode. The generated features must match.

### PR 6 — Cost Model V1

Implement:
- spread cost
- estimated slippage
- missed-fill assumptions
- minimum edge threshold
- net return after costs

### PR 7 — QuestDB Ledger Foundation

Add QuestDB health check, schema creation, and simple write helpers.

QuestDB tables should include:
- model_signals
- risk_decisions
- context_indicator_snapshots
- context_ai_events
- orders
- fills
- trade_outcomes
- latency_metrics
- system_health

Do not add historical market-data ingestion into QuestDB.

### PR 8 — Tiny Ledger Fallback

If QuestDB writes fail, append the event to:

```text
data/emergency_ledger/YYYYMMDD.jsonl
```

Do not build a large replay/spool framework yet.

### PR 9 — Alpaca Execution Metrics Stub

Add order/fill event types and a paper-execution wrapper. Log:
- order sent time
- acknowledgement time
- fill time
- expected price
- fill price
- arrival midprice
- spread
- slippage
- time to fill
- missed fill rate

### PR 10 — Risk Filter V1

Implement deterministic rules:
- minimum model confidence
- max spread
- max latency
- daily loss limit
- duplicate order prevention
- position exposure limit
- event-window block/reduce
- context-risk block/reduce

### PR 11 — ContextState Cache

Create an in-memory latest-state cache that risk reads from directly.

QuestDB is written after updates, but risk does not query QuestDB for live decisions.

### PR 12 — Simple Structured Collectors

Add collectors one at a time:
1. sector/index proxy collector
2. EIA release-window collector
3. FRED rates/yields collector
4. USAspending awards collector
5. calendar events collector

Each collector must:
- update ContextState
- write a ledger snapshot
- handle stale/missing data safely
- never run inside the per-tick decision path

### PR 13 — AI Context Filter V1

Implement strict JSON schema output for text inputs.

Required fields:
- event_time
- source
- affected_tickers
- affected_sector
- event_type
- sentiment
- urgency
- risk_level
- confidence
- valid_from
- valid_until
- summary
- prompt_version
- model_version
- raw_input_hash

Invalid output must be rejected or treated conservatively.

### PR 14 — SEC/News/Social Context Inputs

Start simple. Add SEC EDGAR first, then a basic news/social input stub or one chosen source.

Do not let these sources trade directly.

### PR 15 — Model Inference Interface

Define:

```python
predict(features) -> ModelSignal
```

Use a fake model first so the pipeline can be tested end-to-end.

### PR 16 — Supervised Signal Model

Train a first supervised model using official Databento historical Parquets and cost-aware labels.

Use walk-forward validation, not random splitting.

### PR 17 — Confidence Calibration

Add reliability charts and calibration versioning.

Do not use confidence thresholds seriously until this exists.

### PR 18 — Paper Trading Loop

Connect:
- Databento live/mocked market records
- feature builder
- model inference
- context cache
- risk filter
- Alpaca paper execution
- QuestDB ledger

### PR 19 — Weekly Analysis

Export QuestDB bot/context logs to Parquet and join with official Databento market Parquets.

Reports should analyze:
- approved trades
- blocked signals
- estimated blocked-trade outcomes
- slippage
- latency
- filter usefulness
- context flag usefulness

### PR 20 — Regime and Portfolio Controls

Add:
- drift monitoring
- confidence-distribution monitoring
- slippage/loss circuit breakers
- sector exposure limits
- correlated-name limits
- gross/net exposure limits

### PR 21+ — Later RL Only

Only after the supervised model, cost model, paper trading, and realistic simulator are proven.

RL may be used for:
- exit timing
- hold/reduce/exit
- position sizing
- trade management

Do not use RL as a magic market predictor.

## Coding Standards

- Prefer simple Python modules over complex frameworks.
- Keep functions small and testable.
- Use type hints.
- Add unit tests for every important rule.
- Add integration tests for component boundaries.
- Do not hide errors silently.
- Do not commit generated data.
- Do not commit API keys.
- Do not commit model checkpoints unless explicitly intended.
- Avoid notebook-only logic.
- Avoid large PRs.
- When uncertain, preserve the architecture rules above.

## Pull Request Expectations

Every PR should include:

1. What changed.
2. Why it was needed.
3. How it was tested.
4. What is intentionally not included.
5. Any new config variables.
6. Any risks or follow-up work.

Do not combine unrelated layers in one PR.

Bad PR:
```text
Build full context ingestion, risk engine, model, and live trading.
```

Good PR:
```text
Add EIA release-window collector that updates ContextState and writes QuestDB snapshots.
```

## Safety Defaults

- Default mode must be paper trading.
- Live trading must require an explicit config change.
- If context is stale, risk should reduce or block, not assume conditions are safe.
- If QuestDB ledger write fails, use JSONL fallback.
- If AI context output fails validation, default to neutral or conservative risk behavior.
- If slippage/loss/drawdown exceeds limits, stop new trades.
- If model confidence distribution or feature distribution drifts sharply, reduce size or stop new trades.

## Final Reminder

This is not a generic data platform. It is a simple, measurable trading research and execution system.

Keep the system honest:
- one feature path
- cost-aware labels
- calibrated confidence
- deterministic final risk gate
- full ledger of both approved and blocked signals
- cautious weekly improvements
