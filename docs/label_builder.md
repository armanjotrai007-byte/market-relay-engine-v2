# Label Builder

PR 10 adds the first deterministic label builder for future supervised model
training. It creates cost-aware classification labels from existing feature
snapshots and normalized future midprice observations.

PR 10 does not train a model, run inference, write QuestDB records, connect to
Alpaca, call Databento APIs, read real DBN or Parquet files, or use external
market-calendar services.

## Label Formula

The label flow is:

```text
FeatureSnapshot at T
+ future midprice after horizon
+ PR 9 cost model
= LabelExample.profitable_after_costs
```

The label target is classification-oriented:

```text
profitable_after_costs = net_expected_edge_bps > min_edge_bps
```

The builder uses mid-to-mid movement only. Entry price comes from
`FeatureSnapshot.features["midprice"]`, and the future price comes from a
`ForwardPriceObservation.midprice`. Bid/ask fill prices are not used as entry
or exit prices because PR 9 subtracts spread and slippage separately.

## Horizons And Sides

Supported horizons are fixed to the PR 9 cost model horizons:

- `1m`
- `5m`
- `15m`

The builder maps each horizon explicitly to a `timedelta`; it never adds a raw
string or enum value to a datetime.

Supported sides use the existing `SignalSide` enum:

- `BUY`
- `SELL`

`HOLD`, `EXIT`, and `DO_NOTHING` are rejected because PR 10 labels trade-entry
outcomes only.

## Future Price Selection

`find_forward_price()` selects the earliest same-ticker observation satisfying:

```text
event_time >= snapshot_time + horizon
event_time <= target_time + tolerance
```

It never uses observations before the target horizon time. If no valid forward
price exists, the single-label builder raises `LabelBuilderError`. The
multi-label helper skips missing labels only when
`allow_missing_forward_price=True`.

## Regular Trading Hours

By default, PR 10 enforces regular US equity market hours:

```text
America/New_York, 09:30 to 16:00 inclusive
```

When `enforce_regular_market_hours=True`:

- snapshot time must be within regular hours
- target horizon time must be within regular hours
- a `16:00` target or forward observation is allowed
- observations after `16:00` are rejected
- a near-close target such as `15:58 + 5m = 16:03` fails

This prevents after-hours liquidity from creating normal intraday labels. PR 10
does not implement holidays, early closes, or external market-calendar APIs.

## Cost Model Integration

The label builder calls PR 9 `estimate_cost_from_mid_prices()`. It passes:

- ticker
- BUY or SELL side
- entry midprice
- forward midprice
- horizon
- `spread`, `spread_bps`, and `is_crossed_or_locked` from snapshot features
- order style
- quantity

Cost fields copied into `LabelExample` come directly from the PR 9
`CostEstimate`, including `expected_gross_move_bps`, `total_cost_bps`,
`net_expected_edge_bps`, `min_edge_bps`, `profitable_after_costs`, and
`cost_assumptions_version`.

Crossed or locked snapshots are rejected through the cost model. Missing or zero
spread preserves PR 9 fallback spread behavior.

## No Lookahead Leakage

The label builder consumes an already-built `FeatureSnapshot`. It does not
rebuild features, mutate `FeatureSnapshot.features`, or use future quotes or
spreads as input features. Future observations are used only to assign the
answer label.

## Validation

Run the label builder check without external services:

```powershell
python scripts/check_label_builder.py
```

Run the full local validation suite:

```powershell
python scripts/check_environment.py
python scripts/check_config.py
python scripts/check_contracts.py
python scripts/check_fixtures.py
python scripts/check_historical_parquet.py
python scripts/check_dbn_inspector.py
python scripts/check_feature_builder.py
python scripts/check_feature_parity.py
python scripts/check_cost_model.py
python scripts/check_label_builder.py
python -m pytest
powershell -ExecutionPolicy Bypass -File scripts/run_tests.ps1
```
