# Fake/Paper End-to-End Loop

## Purpose

PR23 adds a deterministic local end-to-end execution wiring check. It proves the current execution path can move from a fake approved trade to order intent, resolved intent, mocked paper response capture, execution-level fill conversion, local portfolio update, and optional broker-position reconciliation.

This is not real broker execution. It exists before real Alpaca paper order submission so local wiring bugs are separated from broker, API, credential, and environment issues.

The PR23 flow is:

```text
fake ModelSignal
+ fake approved RiskDecision
-> OrderIntent
-> reserved OrderManagerState
-> ResolvedOrderIntent
-> mocked AlpacaPaperResponse
-> OrderSubmissionResult
-> fake execution-level fill payload
-> FillEvent
-> PortfolioState update
-> optional BrokerPositionSnapshot reconciliation
-> FakePaperLoopResult
```

## What It Connects

PR23 connects the existing order manager, resolved intent path, mocked Alpaca paper response shape, execution metrics capture, fill reconciliation, and position state helpers.

The fake fill payload is execution-level and carries a unique `execution_id`. It is not derived from a broker order response and does not use aggregate `filled_qty` order payloads.

Reconciliation is intentionally non-circular: the expected broker quantity is calculated from the starting local quantity plus the signed fake fill delta, then the local portfolio is reconciled against that independent snapshot after the fill is applied.

## Safety Exclusions

PR23 does not add:

- Alpaca calls
- order submission
- live trading
- QuestDB writes
- Databento live data
- model inference
- AI calls
- retries
- async services
- schedulers
- external collectors
- new heavy dependencies
- strategy optimization
- backtesting
- profit claims

## Profit Protection Focus

The fake loop protects against wiring mistakes that can become expensive later:

- source signal, risk decision, trace, local order, client order, broker order, and execution IDs stay correlated
- BUY and SELL sides map through intent, result, fill, and position state consistently
- execution-level fill IDs drive fill correlation
- duplicate fill IDs do not double-count local position quantity or realized PnL
- positive slippage means worse execution for both BUY and SELL
- local position state updates from the fill exactly once
- reconciliation mismatches are detected but never auto-corrected
- order-manager reservations are released after the fake fill cycle completes

## Validation

Default validation is offline and requires no Alpaca credentials, QuestDB, model runtime, Databento live feed, or network access:

```powershell
python scripts/check_fake_paper_loop.py
```

It prints exactly:

```text
Fake paper loop check PASS
```

## Next PR

The next recommended PR should be a guarded real Alpaca paper smoke test, not full automation.
