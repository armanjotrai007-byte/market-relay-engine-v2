# Fill / Position Reconciliation

## Purpose

PR22 adds a pure local fill-processing and position-reconciliation layer. PR20 submits Alpaca paper orders. PR21 captures order submission results and optional submit-time `arrival_midprice`. PR22 converts execution-level fill payloads into `FillEvent` records, applies those fills to `PortfolioState`, calculates slippage, and compares local signed quantity against broker signed quantity.

PR22 does not call Alpaca, submit orders, poll, write to QuestDB, trade live, retry failures, start async services, run model inference, call AI services, collect external context, or add new heavy dependencies.

## Execution-Level Fills

`fill_event_from_alpaca_fill_payload(...)` expects one execution-level fill, activity, or trade-update payload with a unique fill identifier from one of these top-level keys:

```text
execution_id, activity_id, id, trade_id
```

PR22 accepts flat execution-level fill/activity payloads where side and symbol are top-level fields. It also accepts Alpaca trade_update-style payloads where the execution-level fill data is top-level but side and symbol/ticker are inside a nested `order` object.

For ticker, the helper uses top-level `symbol` or `ticker`, then nested `order.symbol` or `order.ticker`, then `OrderSubmissionResult.ticker`. For side, the helper uses top-level `side`, then nested `order.side`, and otherwise rejects the payload.

Aggregate order objects are not individual fills. A payload that only has broker `order_id` and aggregate `filled_qty` is rejected because using an order id as `fill_id` can silently drop later partial fills through duplicate-fill protection. The unique fill id must come from an execution, fill, activity, or trade id, not a broker order id or nested `order.id`.

The helper maps order correlation from `OrderSubmissionResult` to `FillEvent.order_id` using `local_order_id`, then `client_order_id`, then `source_signal_id`. It maps `OrderSubmissionResult.source_signal_id` to `FillEvent.model_signal_id` and `OrderSubmissionResult.risk_decision_id` to `FillEvent.risk_decision_id`. `broker_order_id` is not stored on `FillEvent` because the `fill_events` schema does not include that column.

## Slippage

Slippage uses only submit-time expected price metadata:

1. explicit `expected_price` argument
2. `OrderSubmissionResult.arrival_midprice`
3. unavailable

PR22 does not use current market price or fill-time market price. If expected price is missing, zero, negative, NaN, or infinite, `expected_price`, `slippage`, and `slippage_bps` are `None`.

For BUY fills:

```text
slippage = fill_price - expected_price
```

For SELL fills:

```text
slippage = expected_price - fill_price
```

Positive slippage means worse-than-arrival execution for both BUY and SELL.

## Position Application And Reconciliation

`apply_fill_and_reconcile(...)` always delegates to PR19 `apply_fill_to_portfolio(...)`. Duplicate fills are ignored through `PortfolioState.applied_fill_ids`, so repeated `fill_id` values do not double-count quantity or PnL.

`BrokerPositionSnapshot.quantity` follows the PR19 signed quantity convention:

- quantity greater than `0` means long
- quantity less than `0` means short
- quantity equal to `0` means flat

`reconcile_position(...)` compares local quantity against broker quantity within a tolerance. It returns `position_quantity_match` or `position_quantity_mismatch` and never auto-corrects local state.

A broker snapshot passed into `apply_fill_and_reconcile(...)` must be a fresh post-fill or periodic reconciliation snapshot. A stale pre-fill snapshot can produce a temporary false mismatch. PR22 does not fetch broker snapshots and does not decide freshness.

## Health Events

`build_position_reconciliation_health_event(...)` returns a local `SystemHealthEvent` with component `position_reconciliation`, status `OK` for matched results, and status `WARNING` for mismatches. PR22 does not write this event to QuestDB. Future PR23, PR46, or PR47 can decide where and when to log or monitor it.

## Validation

Default validation is offline and requires no Alpaca credentials, QuestDB, or network access:

```powershell
python scripts/check_fill_reconciliation.py
```

It prints exactly:

```text
Fill reconciliation check PASS
```

## Next PR

PR23 will connect the pieces into a fake/paper end-to-end loop.
