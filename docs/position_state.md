# Position and Account State V1

## Purpose

PR19 adds lightweight local position and account state from local `FillEvent`
records.

The flow is:

```text
FillEvent
+ PortfolioState
-> PositionUpdateResult
-> local account and position updates
```

PR19 also resolves PR18 `OrderIntentSide.CLOSE_POSITION` intents into concrete
BUY or SELL directions for later broker adapters. It does not submit orders.

## Signed Quantity Convention

`PositionState.quantity` is signed:

- quantity greater than `0` means long
- quantity less than `0` means short
- quantity equal to `0`, or no position row, means flat

Full closes remove the position from `PortfolioState.positions`. Flat manually
created positions are treated as no active position by helper methods.

## Fill-Based Updates

BUY fills increase signed quantity. SELL fills decrease signed quantity.

Adding to the same side updates weighted average price. Partial closes do not
change the average price of the remaining position.

Examples:

```text
Long 10 @ 100, SELL 4 @ 110
-> realized PnL on 4 shares
-> remaining long 6 @ 100

Short 10 @ 100, BUY 4 @ 90
-> realized PnL on 4 shares
-> remaining short 6 @ 100
```

If a fill crosses zero, PR19 splits the accounting:

```text
Long 10, SELL 15
-> close 10 long and realize PnL
-> open short 5 at the fill price

Short 10, BUY 15
-> cover 10 short and realize PnL
-> open long 5 at the fill price
```

Flip handling is supported for accounting if such fills occur, but PR20 should
not intentionally submit flip orders until broker behavior is validated.

Duplicate `fill_id` values are ignored. A duplicate fill returns
`duplicate_fill=True` and does not mutate quantity or PnL.

## Realized PnL And Daily Loss

`AccountState` separates total and daily realized PnL:

- `total_realized_pnl`: cumulative realized PnL retained across daily resets
- `daily_realized_pnl`: realized PnL for the current trading day or session
- `daily_loss_dollars`: calculated only from `daily_realized_pnl`
- `consecutive_losses`: count of consecutive realized losing reductions

When a fill realizes PnL:

```text
total_realized_pnl += realized_pnl_delta
daily_realized_pnl += realized_pnl_delta
daily_loss_dollars = max(0, -daily_realized_pnl)
```

`daily_loss_dollars` must not be calculated from total realized PnL.

Consecutive loss behavior only changes on closing or reducing fills:

- losing realized PnL increments `consecutive_losses`
- winning realized PnL resets `consecutive_losses` to `0`
- breakeven realized PnL leaves `consecutive_losses` unchanged

`reset_daily_account_state(...)` resets `daily_realized_pnl` and
`daily_loss_dollars`, but does not erase `total_realized_pnl`. A future runner
must call this at the start of a new trading session. `reset_consecutive_losses`
is separate so a caller must explicitly choose when to clear that streak.

## CLOSE_POSITION Resolution

PR18 creates local `OrderIntentSide.CLOSE_POSITION` intents with
`quantity=None`. PR19 resolves them locally:

- long position -> SELL exact absolute quantity
- short position -> BUY exact absolute quantity
- flat or no position -> quantity `0` and reason `no_position_to_close`

Resolved intents never leave quantity as `None`. PR19 does not submit the order;
PR20 will use this state to prepare Alpaca paper orders.

## Risk Inputs And Sector Exposure

`build_risk_state_inputs(...)` produces existing Risk Filter V1 placeholders:

- `AccountRiskInput.daily_loss_dollars` from account daily loss
- `AccountRiskInput.consecutive_losses` from account consecutive losses
- `PortfolioRiskInput.open_positions` from active position count
- `PortfolioRiskInput.symbol_position_exists` from local position state
- `PortfolioRiskInput.duplicate_or_conflicting_order` from optional PR18
  `OrderManagerState`

When no `OrderManagerState` is passed, duplicate/conflict output remains `False`
because PR18 remains the main duplicate/conflict authority.

`sector_exposure(...)` is intentionally minimal. It groups
`abs(quantity) * mark_price` by optional local sector labels and uses `UNKNOWN`
when a ticker has no sector. PR19 adds no sector collector, external API,
correlation logic, or portfolio optimizer.

## Not Included

PR19 intentionally does not add:

- Alpaca
- broker calls
- broker order submission
- live trading
- QuestDB writes
- model inference
- AI calls
- external collectors
- async/background services
- new heavy dependencies
