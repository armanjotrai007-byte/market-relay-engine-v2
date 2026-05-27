# Risk Decision Logging

## Purpose

PR17 adds the simplest useful logging path for deterministic risk decisions.

PR16 decides. PR17 logs.

The risk filter still turns an existing model signal, cost estimate, market
facts, context risk facts, and placeholder account or portfolio facts into a
`RiskDecision`. PR17 adds an opt-in helper that passes that decision to a
generic writer interface.

## Logging Contract

Every model signal should produce a `RiskDecision`, and every `RiskDecision`
should be logged by the caller that owns the runtime flow.

The logging path covers all risk decision types:

- `APPROVE`
- `BLOCK`
- `REDUCE_SIZE`
- `EXIT`
- `DO_NOTHING`

Logging uses a small protocol with a single method:

```python
write_risk_decision(decision, **kwargs)
```

The writer receives the full `RiskDecision` object. That preserves fields such
as `risk_decision_id`, `model_signal_id`, `cost_estimate_id`,
`context_snapshot_id`, reasons, thresholds, and trace ID.

## Failure Handling

`log_risk_decision(...)` returns a `RiskDecisionLogResult`:

- `decision`: the decision that was attempted
- `attempted`: whether a writer call was attempted
- `success`: whether the writer call succeeded
- `error_message`: the writer error text when failures are captured

Logging failures are explicit. By default, logging failures do not raise. The
caller can pass `raise_on_failure=True` for strict validation or tests.

If logging fails for an `EXIT` decision, the `EXIT` decision is still available
on `RiskDecisionLogResult.decision`. Future execution logic can inspect the
result and choose to continue with the exit. PR17 only exposes the result;
PR18 or the runner decides policy.

## Boundaries

Risk logic does not directly depend on QuestDB. The risk package only requires a
generic writer object that implements `write_risk_decision(...)`.

The existing QuestDB ledger writer already has a compatible
`write_risk_decision(...)` method, but PR17 does not add a hard QuestDB
dependency inside risk evaluation.

PR17 does not add:

- JSONL fallback
- retries
- queues
- async or background services
- Alpaca
- live trading
- model inference
- AI calls
- external collectors
- order management
- new heavy dependencies

Next PR:

```text
PR18 - Order Manager V1
```
