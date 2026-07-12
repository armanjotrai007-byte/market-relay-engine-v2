# Testing Fixtures

PR 4 adds reusable fake fixtures under `tests/fixtures/` for future unit tests.
The fixtures instantiate the PR 3 contracts directly and provide stable sample
records for market data, features, model signals, risk decisions, context,
execution, ledger, system health, and composed scenarios.

## Why Fake Fixtures

The project is not ready to commit real Databento DBN samples or encode exact
Databento DBN mappings. Fixture records are intentionally fake but
realistic-looking so future PRs can reuse one consistent test backbone before
source adapters exist.

These fixtures use the project's generic `MarketRecord` contract fields. They
are fake test records and do not represent exact Databento DBN schema field
names or field mappings. Real Databento DBN inspection and source-to-contract
mapping will be handled in later PRs.

Do not commit real DBN files, compressed DBN files, API exports, or generated
market-data archives. Future local Databento files should live under ignored
local folders such as `data/raw/`, which preserves local inspection workflows
without making real market data part of the repository.

## Fixture Shape

Fixture modules primarily expose factory functions. Constants are reserved for
stable IDs, stable timestamps, and simple fixed values.

Fixture IDs are deterministic and visibly fake:

```text
FIXTURE-{PREFIX}-{INDEX:04d}
```

Examples include `FIXTURE-SIGNAL-0001`, `FIXTURE-ORDER-0001`,
`FIXTURE-FILL-0001`, and `FIXTURE-CONTEXT-0001`.

## Scenarios

Scenario fixtures are plain dictionaries with a consistent key set. Missing
records are represented with `None` or an empty list.

- `approved_oil_trade_scenario()` represents an approved XOM-style paper trade
  with order, fill, outcome, latency, and healthy system records.
- `blocked_defense_trade_scenario()` represents a defense-sector signal blocked
  before order creation.
- `reduced_size_context_risk_scenario()` represents a context-risk case where
  a future risk gate would reduce size.
- `latency_slippage_warning_scenario()` represents an approved trade with
  elevated latency, slippage, and warning health records.
- `stale_context_block_scenario()` represents conservative blocking when
  context is stale or expired.

## Validation

Run fixture validation locally:

```powershell
python scripts/check_fixtures.py
```

The standard local validation runner also includes fixture checks:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_tests.ps1
```

`check_fixtures.py` imports the fixture modules, builds representative records,
serializes them through the PR 3 JSON helpers, checks UTC `Z` timestamp strings,
checks non-empty IDs, and statically rejects direct imports of obvious
external-service/client modules.

PR34 context fixtures construct classification event type, risk level, urgency,
status, and shadow action fields with their strict enum types rather than loose
strings. Tests cover generated IDs, UTC enforcement, defensive collection
copies, exact enum serialization, status-specific response shapes, validation
results, and shadow size-factor rules. Form 4 purchase/sale values use the
separate deterministic enum and are rejected by AI-classification contracts;
the actual deterministic parser remains deferred to PR38.

All Phase 7 fixture source text, IDs, and hashes are synthetic. Fixtures do not
contain real filings, articles, social posts, API output, credentials, or
provider exceptions.

