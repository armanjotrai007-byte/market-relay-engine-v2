# Order Manager V1

## Purpose

PR18 adds a lightweight local order-intent layer.

The flow is:

```text
ModelSignal
+ RiskDecision
+ risk log success/failure
+ desired quantity
+ in-memory order state
-> OrderManagerResult
```

PR16 decides risk. PR17 logs risk decisions. PR18 converts those logged risk
decisions into safe local order intents.

`OrderIntent` is not a broker order. PR18 does not call Alpaca, submit paper or
live orders, write to QuestDB, run model inference, call AI services, collect
external context, or start background services.

## Local Intent Sides

PR18 uses a local `OrderIntentSide` enum:

- `BUY`
- `SELL`
- `CLOSE_POSITION`

`CLOSE_POSITION` is a local intent, not a broker side. Brokers accept concrete
buy/sell style order sides, so PR19/PR20 will translate close-position intent
after position state exists:

- long position -> sell
- short position -> buy or buy-to-cover
- no position -> no broker order or safe no-op

## Quantity Policy

Entry intents use the caller's `desired_quantity` or the configured default.

`REDUCE_SIZE` preserves fractional quantities:

```text
effective_quantity = desired_quantity * reduce_size_factor
```

PR18 intentionally does not round, floor, apply whole-share rules, or apply
broker-specific lot-size constraints. Those rules belong in later
position/broker/execution adapter logic.

`CLOSE_POSITION` intents use `quantity=None`. That means "close or liquidate the
full position once position state or a broker adapter exists." PR18 does not
have enough state to know the liquidation quantity.

## Safety Checks

Entry intents require successful risk logging by default. If risk logging fails,
`BUY` and `SELL` entries are blocked with `risk_log_failed`.

`EXIT` decisions become `CLOSE_POSITION` intents. They may proceed when risk
logging failed if `allow_exit_when_risk_log_failed=True`. This is intentional:
emergency exit safety has priority over audit completeness.

When an unlogged exit proceeds, later order/fill ledger records may reference a
`risk_decision_id` that was not successfully persisted. Weekly analysis must
handle unpersisted or orphaned risk-decision IDs. PR17 exposes log
success/failure, and PR18 preserves that policy decision.

The order manager blocks:

- duplicate signal IDs
- duplicate same-side entry orders
- conflicting buy/sell entry orders
- max open-order count per symbol
- new entries while a `CLOSE_POSITION` reservation is active
- duplicate close-position attempts for the same ticker
- invalid entry quantities

`CLOSE_POSITION` bypasses normal buy/sell duplicate, conflict, max-open-order,
and quantity checks. Downstream execution should cancel or ignore working orders
and prioritize close-position behavior. PR18 only creates the local intent.

## State Reservation

`build_order_intent(...)` is pure and does not mutate state.

Call `reserve_order_intent(...)` immediately after receiving an allowed intent
and before any broker/API call. Reservation marks the source signal ID as used
and adds an in-memory `OpenOrderState` placeholder for `BUY`, `SELL`, and
`CLOSE_POSITION` intents. Close-position reservations may have `quantity=None`.

Call `release_open_order(...)` when a reserved order is completed, rejected,
canceled, or otherwise resolved. Releasing an order removes the open-order
placeholder but does not unmark the source signal ID. A completed or rejected
order should not allow the exact same old signal to be reused.

## Next PR

PR19 - Position and Account State V1.
