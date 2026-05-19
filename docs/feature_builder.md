# Canonical Feature Builder V1

PR 7 adds the first canonical feature calculation path:

```text
MarketRecord -> feature_builder.py -> FeatureSnapshot
```

Both historical training/backtesting and future live trading must use this same
feature builder. This prevents separate historical and live feature logic from
drifting apart.

PR 7 is source-agnostic. It consumes already-normalized `MarketRecord` objects
only. It does not parse DBN files, read Parquet files, call Databento APIs,
write QuestDB records, train models, run inference, connect to Alpaca, or place
trades.

## Output Contract

The existing `FeatureSnapshot` contract is unchanged. V1 feature values live
inside the `features` dictionary:

```python
FeatureSnapshot(
    snapshot_time=...,
    ticker=...,
    feature_version="feature_v1",
    features={...},
    source_record_count=...,
    lookback_window_seconds=...,
)
```

The V1 feature key set is stable and defined in code as `V1_FEATURE_KEYS`:

```text
ticker
record_count_window
trade_count_window
quote_count_window
last_price
last_trade_price
last_trade_size
last_bid_price
last_ask_price
last_bid_size
last_ask_size
midprice
spread
spread_bps
is_crossed_or_locked
lookback_window_seconds
volume_window
price_return_window
midprice_return_window
midprice_change_from_previous
simple_volatility_window
```

Feature values are JSON-safe. The builder does not emit NaN, Infinity,
datetimes, enums, or custom objects inside `FeatureSnapshot.features`.
Non-finite numeric inputs are normalized to `None`.

## Quote Normalization

The feature builder owns basic quote normalization:

- finite `record.midprice` is used first
- otherwise midprice is computed from finite `bid_price` and `ask_price`
- finite `record.spread` is used first
- otherwise spread is computed from finite `bid_price` and `ask_price`
- `spread_bps` is computed only when midprice is finite and positive
- `is_crossed_or_locked` is true when finite bid and ask exist and bid >= ask

This is basic market normalization only. PR 7 does not create a production DBN
adapter or map Databento schema-specific fields into `MarketRecord`.

## Record Types

Record classification is intentionally small and source-agnostic.

Trade-like records include record types such as `trade`, `trades`, and
`tbbo_trade`, or records with finite `price` and `size` that do not also carry
bid/ask values.

Quote-like records include record types such as `quote`, `bbo`, `mbp-1`,
`tbbo`, `bbo-1s`, and `bbo-1m`, or records with finite bid and ask fields.

Unrecognized record types, including future normalized `status`, `definition`,
`statistics`, or `imbalance` records, still contribute to
`record_count_window`. They do not contribute to trade or quote counts and do
not update trade, quote, midprice, or spread features unless they contain usable
normalized trade or quote fields.

## Rolling Window

The rolling window is based on event time, not wall-clock time. For each ticker,
the stateful builder maintains `max_event_time_seen`. Pruning uses:

```text
max_event_time_seen - lookback_window_seconds
```

This keeps the window moving forward even when a slightly delayed record arrives
after a newer record.

The update order is:

1. validate the incoming `MarketRecord`
2. append it to the ticker deque
3. update `max_event_time_seen`
4. prune by event time
5. enforce `max_records_per_ticker`

`max_records_per_ticker` defaults to `50000`. It is an out-of-memory safety cap,
not the primary window definition. Time pruning happens before record-count
capping.

## Batch vs Stateful Use

`build_feature_snapshot(records, ...)` is a batch convenience helper. It sorts
records by `event_time`, then feeds a temporary `FeatureBuilder`. It is intended
for tests and historical/batch-style processing.

`FeatureBuilder.update(record)` is the stateful live-style path. It processes
records in caller arrival order and does not secretly sort them. It still uses
`max_event_time_seen` so the active rolling window moves forward only.

PR 8 parity tests must account for this difference. When comparing the batch
helper against the stateful path, feed records in event-time order unless the
test is specifically checking out-of-order arrival behavior.

## Validation

Run the feature builder check without external services:

```powershell
python scripts/check_feature_builder.py
```

Run the full validation suite:

```powershell
python scripts/check_environment.py
python scripts/check_config.py
python scripts/check_contracts.py
python scripts/check_fixtures.py
python scripts/check_historical_parquet.py
python scripts/check_dbn_inspector.py
python scripts/check_feature_builder.py
python -m pytest
powershell -ExecutionPolicy Bypass -File scripts/run_tests.ps1
```

Future PR 8 will formalize historical/live feature parity tests. PR 9 is
reserved for the cost model, and PR 10 is reserved for labels.
