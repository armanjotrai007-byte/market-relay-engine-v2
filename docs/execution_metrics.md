# Execution Metrics / Order Result Capture

## Purpose

PR21 adds a lightweight local capture layer for order-submission results. PR20
submits a resolved paper order and returns `AlpacaPaperResponse`; PR21 combines
that response with the original PR19 `ResolvedOrderIntent`, caller-provided local
timestamps, and optional submit-time market metadata.

The flow is:

```text
ResolvedOrderIntent
+ AlpacaPaperResponse
+ local submit timestamps
+ optional arrival_midprice
-> OrderSubmissionResult
-> future order_events / latency_metrics payloads
```

PR21 does not submit orders, call Alpaca, write to QuestDB, compute fills,
reconcile broker state, retry failures, trade live, run model inference, call AI
services, collect external context, or add new heavy dependencies.

## Local Clock Latency

Network and order-submission latency is measured only from local timestamps:

```python
latency_ms = (submit_completed_at - submit_started_at).total_seconds() * 1000.0
```

The timestamps are supplied by the caller and must be timezone-aware UTC values.
`latency_ms` must be finite and greater than or equal to `0.0`; `0.0` is valid
for mocked tests and fail-fast local paths.

PR21 does not use Alpaca or broker timestamps to calculate latency. Broker
timestamps, if captured in a future PR, are audit metadata only because mixing
local and broker clocks can introduce clock-drift errors.

Any future wrapper that captures timestamps should use the project UTC helpers in
`market_relay_engine.common.time`, not bare `datetime.now()`.

## Order Result Capture

`OrderSubmissionResult` links local intent identity to the broker response:

- `broker_order_id` maps to future `order_events.broker_order_id`.
- `client_order_id`, `local_order_id`, and `source_signal_id` preserve
  idempotency and correlation.
- `status_code` and `error_message` preserve broker/network failure context for
  local analysis without storing the full raw response.
- `order_type` is normalized to canonical `OrderType` contract values before it
  is stored or emitted in payloads. Missing order type metadata falls back to
  `OrderType.MARKET.value` (`"MARKET"`); unsupported non-empty string values fail
  capture instead of writing mixed casing into `order_events.order_type`.
- `raw_response` is never stored in `OrderSubmissionResult`.

PR21 expects a PR19 `ResolvedOrderIntent`. PR20 should not submit unresolved
`CLOSE_POSITION` intents, so PR21 does not accept unresolved close-position
capture. If submission never happened because PR20 raised a local
`AlpacaPaperError`, PR21 may not produce a capture record yet; a future PR can add
local rejection telemetry if needed.

## Client Order ID Fallback

`capture_order_submission_result(...)` resolves `client_order_id` with this
precedence:

1. explicit `client_order_id` argument
2. safe `raw_response.get("client_order_id")`
3. `local_order_id` argument
4. `getattr(intent, "order_id", None)`
5. `intent.source_signal_id`

Raw response access is defensive:

```python
raw_client_order_id = (
    raw_response.get("client_order_id") if isinstance(raw_response, dict) else None
)
```

Empty strings and non-string raw values are ignored.

## Arrival Midprice

`arrival_midprice` is optional submit-time market metadata. It should come from
the active `FeatureSnapshot` / `MarketRiskInput` at submit time, before the order
response is captured.

PR21 does not compute slippage. PR22 or a later fill-reconciliation PR can combine
`arrival_midprice` with future `fill_price` data.

The existing `order_events` schema does not have an `arrival_midprice` column.
PR21 stores `arrival_midprice` on `OrderSubmissionResult`, then maps it to the
schema-compatible `expected_price` field in `build_order_event_payload(...)`.
For market orders, `submitted_price` remains `None` unless an actual submitted or
limit price is available from a future input.

## Payload Helpers

`build_order_event_payload(...)` prepares a schema-compatible dictionary for the
existing `order_events` writer path. It only emits accepted `order_events` column
names and does not include `arrival_midprice`, `client_order_id`, `status_code`,
`error_message`, `submit_started_at`, or `submit_completed_at`. The emitted
`order_type` value is the canonical uppercase contract value, such as `MARKET`.
`order_time` is the local submit/send timestamp from `submit_started_at`.

`build_latency_metric_payload(...)` prepares a schema-compatible dictionary for
the existing `latency_metrics` writer path. It only emits accepted
`latency_metrics` column names:

```text
measured_time, write_time, latency_metric_id, component, source, latency_ms,
ticker, event_type, run_id, session_id, schema_version, trace_id
```

The latency event name is stored in `event_type` as
`alpaca_order_submit_latency_ms` to match the existing QuestDB schema. The helper
uses `component="execution"` and `source="alpaca_paper"`; it does not emit a
`metric_name` key because that column does not exist. `measured_time` is the local
response/completion timestamp from `submit_completed_at`.

Both helpers are capture-only. They do not write to QuestDB.

## Validation

Default validation is offline and requires no Alpaca credentials, QuestDB, or
network access:

```powershell
python scripts/check_execution_metrics.py
```

It prints exactly:

```text
Execution metrics check PASS
```
