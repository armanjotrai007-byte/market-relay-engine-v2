# Risk Filter V1

## Purpose

PR16 adds the first deterministic risk filter. It is the final local gate after
a model signal and cost estimate already exist.

The PR16 flow is:

```text
ModelSignal
+ CostEstimate
+ market quality inputs
+ generic context risk inputs
+ account/portfolio placeholder inputs
-> RiskDecision
```

Risk Filter V1 is intentionally simple, deterministic, and config-driven. It
does not run model inference, call AI services, place broker orders, write to
QuestDB, or collect external context.

## Inputs

`evaluate_risk(...)` requires:

- `signal`: existing `ModelSignal`
- `market`: `MarketRiskInput`
- `evaluation_time`: timezone-aware UTC datetime used as `decision_time`

For BUY and SELL entry signals, PR16 also enforces PR9 cost output when
`execution_quality.reject_if_expected_edge_below_cost` is true:

- missing `CostEstimate` blocks the entry
- non-profitable `CostEstimate` blocks the entry
- ticker mismatch between signal and cost estimate blocks the entry

`HOLD`, `DO_NOTHING`, and `EXIT` do not require a cost estimate.

Market staleness is deterministic:

```text
market_data_age_seconds = evaluation_time - market.market_data_time
```

The risk filter does not call wall-clock time inside `evaluate_risk`.

## Rules

Rules run in a fixed order:

1. HOLD or DO_NOTHING returns `DO_NOTHING`.
2. EXIT returns `EXIT` and bypasses entry checks.
3. BUY/SELL cost estimate checks.
4. Confidence.
5. Spread.
6. Latency.
7. Market data staleness.
8. Account placeholder limits.
9. Portfolio placeholder limits.
10. Context risk.
11. Otherwise approve.

The first blocking rule wins. There is no score, model, registry, plugin
system, or background service.

## Context

PR16 context input is generic risk state only:

- event window active
- high context risk active
- elevated context risk active
- optional context snapshot ID
- optional machine-readable reasons

The adapter `context_risk_input_from_contracts(...)` maps existing PR3
`ContextStateSnapshot` and `ContextFlag` contracts into these generic booleans.
It ignores expired flags and only looks at generic fields like `risk_level`,
`severity`, `flag_type`, and `valid_until`.

It does not understand EIA, FRED, SEC, USAspending, yfinance, news, social
feeds, or AI-specific payloads. Future collectors should translate their
outputs into these generic facts before risk evaluation.

PR34 does not broaden this adapter. Phase 7 `ContextAIEvent`, Phase 7-enriched
`ContextFlag`, classification, validation, and
`ShadowContextPolicyEvaluation` records are research-only and are not routed
into `evaluate_risk(...)` or `approved_risk_context`. A hypothetical shadow
action such as `BLOCK`, `REDUCE_SIZE`, or `DELAY` records what a future research
policy would have proposed; it cannot change the real `RiskDecision`.

The existing deterministic EIA release-window flag path remains compatible and
unchanged. Its `available_at` metadata describes public availability; the
pre-release active window remains a separate deterministic risk fact.

## REDUCE_SIZE Contract

`RiskDecisionType.REDUCE_SIZE` uses `approved=True`, but it is not equivalent to
full approval.

Consumers must inspect `decision.decision`, not only `decision.approved`.

- `decision == APPROVE`: full-size entry can proceed.
- `decision == REDUCE_SIZE`: entry is conditionally approved at the reduced
  size only.
- `decision == BLOCK` or `DO_NOTHING`: no entry should be placed.

Downstream execution must apply `reduce_size_factor` for REDUCE_SIZE. PR16 uses
an internal V1 default of `0.5` for elevated context risk. This can be made
configurable in a later PR.

## Thresholds

Risk Filter V1 loads thresholds from `config/risk_limits.yaml`:

- minimum model confidence
- max spread dollars and bps
- max latency
- stale market-data seconds
- cost-estimate enforcement toggle
- max daily loss and consecutive losses
- max open positions and per-symbol placeholder limits
- event/high/elevated context risk settings

`thresholds_used` records only the rule that caused the decision and relevant
actual/limit values. It does not dump every config threshold into every
decision.

## Not Included

PR16 intentionally does not add:

- Alpaca
- broker execution
- QuestDB writes
- live trading
- model inference
- model training
- AI calls
- external collectors
- raw market-data storage
- order manager
- full account state
- full portfolio state
- async/background services
- new heavy dependencies

PR17 adds opt-in Risk Decision Logging through a separate writer interface.
