# Data Contracts

PR 3 defines the first internal record shapes for Market Relay Engine V2. These
contracts are intentionally lightweight frozen dataclasses with explicit fields.
They are stable shapes for future components, not a framework, registry, ORM, or
schema engine.

## Purpose

The contracts standardize how future layers will pass and log facts:

- market records from future Databento adapters
- feature snapshots from the future canonical feature builder
- model signals, including blocked or ignored signals
- deterministic risk decisions
- structured context indicators, AI context events, and context flags
- order and fill events for paper execution metrics
- trade outcomes, latency metrics, and system health records

PR 3 does not implement live integrations, broker execution, feature
calculations, model inference, risk logic, QuestDB writes, or AI calls.

## Timestamp Standard

All datetime values must be timezone-aware UTC. Naive datetimes are rejected so
records cannot silently mix local time and UTC. JSON serialization emits UTC ISO
strings ending in `Z`.

Common timestamp meanings:

- `event_time`: canonical time for the record or event.
- `source_event_time`: timestamp from the upstream source, if different.
- `local_receive_time`: when the local process received the upstream event.
- `snapshot_time`: when a feature or context snapshot was created.
- `signal_time`: when a model signal was produced.
- `decision_time`: when the deterministic risk decision was produced.
- `order_time` and `fill_time`: execution event timestamps.
- `measured_time`: when a latency metric was measured.
- `write_time`: future ledger write timestamp, not implemented in PR 3.

## IDs and Tracing

Runtime IDs and contract record IDs are standard-library UUID strings with short
log-safe prefixes. Callers may pass IDs explicitly, or contract defaults create
the record IDs that are needed later for ledger joins and weekly analysis.

- `run_id`: one process or validation run.
- `session_id`: one paper/live session.
- `trace_id`: correlates related records across components.
- Record IDs include `feature_snapshot_id`, `signal_id`, `risk_decision_id`,
  `context_event_id`, `context_flag_id`, `order_id`, `fill_id`, `outcome_id`,
  `latency_metric_id`, and `health_event_id`.

## Contract List

- `MarketRecord`: generic market record for future Databento-derived adapters.
- `FeatureSnapshot`: feature dictionary output shape, without calculations.
- `ModelSignal`: model output shape, without inference.
- `RiskDecision`: deterministic risk decision shape, without risk logic.
- `ContextIndicatorSnapshot`: structured context indicator snapshot.
- `ContextAIEvent`: structured AI-context output shape, without AI calls.
- `ContextFlag`: risk flag shape for future context and risk layers.
- `OrderEvent`: order event shape, without broker placement.
- `FillEvent`: fill event shape, without broker integration.
- `TradeOutcome`: future trade result and return measurement shape.
- `LatencyMetric`: component latency measurement shape.
- `SystemHealthEvent`: system health record shape, without monitoring loops.

## Serialization

`market_relay_engine.common.serialization` provides simple JSON helpers:

- dataclasses serialize to dictionaries
- enums serialize to readable values
- datetimes serialize to UTC ISO strings
- nested lists and dictionaries are supported
- `from_json_string()` returns a plain dictionary only

PR 3 intentionally does not reconstruct dataclass instances from JSON. That
would add enum mapping and datetime parsing policy before those requirements are
needed.

## Validation

Run local validation with:

```powershell
python scripts/check_environment.py
python scripts/check_config.py
python scripts/check_contracts.py
python -m pytest
powershell -ExecutionPolicy Bypass -File scripts/run_tests.ps1
```
