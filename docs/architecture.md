# Architecture

Market Relay Engine V2 is a local, testable trading research and future paper/live execution system with two deliberately separate context paths.

```text
Official Databento historical Parquet/DBN
-> normalized MarketRecord
-> canonical feature builder
-> supervised training and backtesting

Databento live DBN
-> normalized MarketRecord
-> same canonical feature builder
-> calibrated model signal
-> deterministic Python risk gate
-> Alpaca paper execution
```

The current structured collectors update an in-memory `ContextStateCache` outside the per-tick loop. `DecisionContextAssembler` projects visible structured evidence for research and audit, but does not call collectors, read QuestDB, or change a `RiskDecision`.

Phase 7 adds a separate research-only path:

```text
trusted-code-created raw-input metadata (source authorization deferred to PR35)
-> normalized source-document metadata and hashes
-> bounded in-memory classification request where AI is needed
-> strict classification response and validation
-> research-only ContextAIEvent / ContextFlag
-> future research cache
-> hypothetical ShadowContextPolicyEvaluation
```

PR34 defines contracts and ledger metadata only. It does not call Gemini, collect SEC filings, archive documents, populate a research cache, execute a shadow policy, or route Phase 7 records into `approved_risk_context` or the real risk filter. Form 4 purchase/sale events use a separate deterministic vocabulary and remain deferred to PR38; they are never Gemini classification values.

Provider failures expose only a safe category and summary to contracts and QuestDB. PR36 must retain full exception details in ignored local structured logs correlated by `classification_attempt_id`; exception text and tracebacks do not belong in QuestDB.

QuestDB is the bot ledger and black-box recorder. It stores IDs, hashes, concise summaries, validation metadata, shadow results, signals, risk decisions, orders, fills, latency, slippage, PnL, outcomes, and health. It is not a historical market-data warehouse and must not store full filings, articles, posts, normalized documents, request excerpts, prompts, credentials, or secrets.

The deterministic Python risk gate remains the only final pre-trade authority. AI and external context have no direct trade, block, delay, or sizing authority in PR34.
