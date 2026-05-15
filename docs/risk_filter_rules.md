# Risk Filter Rules

The final pre-trade authority will be a deterministic Python risk gate.

Future risk inputs may include model confidence, spread, latency, event windows, context flags, portfolio state, daily loss state, open orders, and calibration state.

PR 1 only includes placeholder config defaults. These values are not optimized live-trading settings.

Default posture:

- paper trading by default
- block or reduce when data is stale or unsafe
- never let AI directly approve trades
- never enable live trading by default
