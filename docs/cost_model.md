# Cost Model V1

PR 9 adds the first deterministic cost model for Market Relay Engine V2. It is
a pure calculation layer between feature building and future cost-aware labels.
It estimates whether a hypothetical expected move is large enough after spread,
round-trip slippage, size penalty, missed-fill risk, and the minimum edge buffer.

PR 9 is not the risk filter, label builder, model training, Alpaca execution,
QuestDB logging, live data, or an external-service integration.

## Inputs

The cost model uses raw values, not `FeatureSnapshot`. Callers should extract
feature values before calling the module.

Expected gross move is mid-to-mid:

```text
BUY  = (exit_midprice / entry_midprice - 1) * 10000
SELL = (entry_midprice / exit_midprice - 1) * 10000
```

Do not pass bid/ask fill prices as entry and exit midprices. Spread is
subtracted separately as a cost, so using fill prices would double-count spread.

Supported horizons are:

- `1m`
- `5m`
- `15m`

Supported sides use the existing `SignalSide` enum:

- `BUY`
- `SELL`

SELL math support does not mean a ticker is shortable. Actual shortability and
tradability checks belong to later risk and execution PRs.

## Default Assumptions

`CostModelConfig` defaults to:

```text
min_edge_bps = 1.0
round_trip_slippage_per_share = 0.02
market_order_spread_multiplier = 1.0
limit_order_spread_multiplier = 0.0
limit missed-fill probability by horizon:
  1m = 0.30
  5m = 0.20
  15m = 0.10
size_penalty_bps_per_1000_shares = 0.25
size_penalty_free_quantity = 100.0
fallback_minimum_spread_bps = 1.0
assumptions_version = cost_model_v1
```

The default round-trip slippage is `$0.02/share`: `$0.01` adverse on entry and
`$0.01` adverse on exit.

## Formula

For each estimate:

```text
spread_cost_bps
+ estimated_slippage_bps
+ size_penalty_bps
= base_cost_bps

expected_gross_move_bps - base_cost_bps
= pre_missed_fill_net_edge_bps

missed_fill_penalty_bps
= missed_fill_probability * max(pre_missed_fill_net_edge_bps, 0)

base_cost_bps + missed_fill_penalty_bps
= total_cost_bps

expected_gross_move_bps - total_cost_bps
= net_expected_edge_bps
```

The profitability target is classification-oriented:

```text
profitable_after_costs = net_expected_edge_bps > min_edge_bps
```

`exceeds_min_edge_threshold` uses strict greater-than. If net edge exactly
equals `min_edge_bps`, it is false.

## Order Styles

`MARKET` assumes full spread cost and zero missed-fill probability.

`LIMIT_AT_MID` assumes no direct spread-crossing cost in V1, but missed-fill
probability depends on the horizon. Slippage still applies through
`round_trip_slippage_per_share`.

No other order styles are supported in PR 9.

## Spread Safety

Crossed or locked books are rejected when `is_crossed_or_locked=True`.
Negative spread or spread bps is rejected.

If spread bps is zero or missing, the estimate applies
`fallback_minimum_spread_bps` instead of treating spread as free. The output
sets `fallback_spread_applied=True` and records the fallback reason.

## Validation

Run the cost model check without external services:

```powershell
python scripts/check_cost_model.py
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
python -m pytest
powershell -ExecutionPolicy Bypass -File scripts/run_tests.ps1
```
