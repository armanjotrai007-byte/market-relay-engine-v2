# Historical/Live Feature Parity

PR 8 adds small validation helpers that prove historical-style and live-style
feature generation can share the same canonical feature builder:

```text
MarketRecord -> FeatureBuilder -> FeatureSnapshot
```

The goal is to prevent train/live skew. Historical training, backtesting, and
future live trading must not grow separate feature logic that computes different
values from equivalent normalized market records.

## Same Builder Rule

Both parity paths consume only normalized `MarketRecord` objects and feed them
through the PR 7 `FeatureBuilder`. PR 8 does not parse Databento DBN files, read
real Parquet data, call external APIs, write QuestDB rows, train models, run
inference, connect to Alpaca, or trade.

The historical-style helper sorts records by `event_time` before updating the
builder. This mirrors batch processing, where historical files can be sorted by
market event time before feature generation.

The live-style helper processes records in exact caller order. It does not sort
and it does not reject out-of-order records. This preserves the production-style
`FeatureBuilder.update(record)` behavior from PR 7: live feeds arrive in caller
order, and the builder tracks `max_event_time_seen` so delayed records do not
move the rolling window backward.

## Formal Parity Requirement

Formal parity comparison requires equivalent event-time-ordered inputs:

```text
historical records -> stable sort by event_time -> FeatureBuilder
live records already in event-time order -> FeatureBuilder.update(...)
```

Out-of-order live arrival is supported by `FeatureBuilder`, but it is a separate
behavior category. PR 8 must not compare sorted historical data against
unsorted live arrival data and call any difference a parity failure.

Same-timestamp records are realistic. Identical `event_time` values are allowed,
and Python sorting is stable, but equal timestamps do not define a unique order
by themselves. Deterministic parity for same-timestamp records requires the
historical and live inputs to preserve the same relative order for those
records.

## Semantic Comparison

Semantic parity compares market-derived deterministic fields such as
`snapshot_time`. It ignores generated IDs. If future processing-time fields are
added, they should be excluded from parity comparison because historical batch
generation time and live processing time naturally differ.

The comparison includes:

- `ticker`
- `snapshot_time`
- `feature_version`
- `source_record_count`
- `lookback_window_seconds`
- `schema_version`
- stable `trace_id` values when present
- feature dictionary keys and values

The comparison excludes:

- `feature_snapshot_id`

Float values are compared with a tight `math.isclose()` tolerance. NaN and
Infinity are rejected because feature values must be deterministic and JSON
safe.

`feature_snapshot_semantic_dict()` exists for tests and validation scripts as
testing support, not a stable production API. Future PRs should not build
runtime business logic around its exact dictionary shape.

## What PR 8 Proves

PR 8 proves that event-time-ordered historical and live-style processing can
produce equivalent final `FeatureSnapshot` objects through the same canonical
builder. This is the foundation for later model training and live inference to
share one feature surface.

## What PR 8 Does Not Prove

PR 8 does not prove exact Databento schema mappings, DBN decoding, Parquet
production readiness, out-of-order live jitter policy beyond the existing PR 7
builder behavior, model quality, label quality, execution safety, or risk
filter correctness.

Future PRs build on this in order:

- PR 9: Cost Model V1
- PR 10: Labels
- later model training
- later live Databento adapter

## Validation

Run the parity check without external services:

```powershell
python scripts/check_feature_parity.py
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
python -m pytest
powershell -ExecutionPolicy Bypass -File scripts/run_tests.ps1
```
