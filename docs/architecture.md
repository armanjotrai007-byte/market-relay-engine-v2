# Architecture

Trading System V2 is built as a small, local, testable Python project. PR 1 only defines the repository shape and safe defaults.

Future flow:

```text
Databento market data
-> canonical feature builder
-> calibrated signal model
-> deterministic Python risk gate
-> Alpaca paper execution
-> QuestDB bot ledger
```

Structured context and AI-interpreted context will become risk flags. AI context never approves or places trades directly.

QuestDB is for bot ledger records only: model signals, risk decisions, context flags, orders, fills, latency, slippage, PnL, outcomes, and system health. It is not a historical market-data warehouse.

PR 1 does not connect to any external service.
