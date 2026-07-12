# Market Relay Engine V2 Handoff

## Current work

Repository: `armanjotrai007-byte/market-relay-engine-v2`

PR34 review branch:

```text
pr34-phase7-contracts-repository-consistency
```

Base `main` SHA:

```text
0f79eda1170237666a61fe2f2767e7eb5141200d
```

PR34 reconciles current source configuration and documentation, defines the
Phase 7 provider-neutral context contracts and strict enums, and adds QuestDB
metadata schemas for classification attempts and hypothetical shadow policy
evaluations. It is an offline foundational-contract PR: it does not implement
Gemini, SEC collection, a research cache, a shadow-policy runtime, or manual
news/social ingress.

## Non-negotiable boundaries

- Historical market truth is official Databento DBN/Parquet-derived data, not
  QuestDB.
- Historical and live paths use the same canonical feature builder.
- Live decisions read context from memory, never QuestDB in the per-tick or
  per-signal loop.
- QuestDB stores bot facts and audit metadata, not raw market or context bodies.
- AI context is structured and research-only. It cannot approve, block, resize,
  delay, or place a real trade.
- The deterministic Python risk filter remains the final pre-trade authority.
- Alpaca remains paper-first; live trading is disabled.
- yfinance remains development-only and non-production-critical.
- No credentials, `.env`, generated data, full provider exceptions/tracebacks,
  or raw source documents belong in Git or QuestDB. Safe failure
  category/summary metadata is allowed.

Historical replay still uses batch sorting vs live arrival order: historical
feature replay sorts by event time, while live processing preserves arrival
order. Both paths continue to use the same canonical feature builder.

## Current configuration

The final tradable universe is:

```text
PLTR LMT RTX GD AVAV XOM OXY SLB COP VLO
```

EIA, FRED, USAspending, the local macro calendar, and the yfinance development
proxy are intentionally enabled for bounded explicit collection outside the
decision loop. Enablement does not grant trading authority or schedule calls.
SEC EDGAR, news, social, and the AI context filter remain disabled in repository
configuration.

Repository history shows FRED was intentionally enabled with the other built
structured sources. PR34 therefore repairs the stale disabled-by-default unit
test rather than disabling valid current configuration.

## Phase 7 contract flow

```text
ContextRawInput
-> ContextSourceDocument
-> ContextClassificationRequest
-> ContextClassificationResponse
-> ContextValidationResult
-> research-only ContextAIEvent / ContextFlag
-> future research cache
-> ShadowContextPolicyEvaluation
```

Gemini-classification event types contain `UNKNOWN`, `OTHER`, and the bounded
SEC 8-K values, with `UNKNOWN` reserved for non-valid response shapes. Form 4
open-market purchase/sale values use a separate deterministic enum and cannot
enter the classification response; their parser remains PR38 work.

`available_at` means the earliest trusted demonstrable public availability of
the underlying fact. When both top-level `ContextFlag.available_at` and legacy
`details["provenance"]["available_at"]` exist, adapters require equal UTC
instants. EIA preserves its existing pre-release risk window while placing the
official release time in both availability representations.

Only a safe provider failure category/summary may enter contracts and QuestDB.
PR36 must retain full exceptions locally in ignored structured logs correlated
by the same classification attempt ID.

## QuestDB deployment

PR34 adds:

```text
context_classification_attempts
shadow_context_policy_evaluations
```

It also appends nullable lineage/source/hash/timestamp metadata to
`context_ai_events` and `context_flags`. The committed reset schema is
destructive and must not be used to upgrade a persistent server.

After merge and before starting a PR34 writer, stop writers and apply:

```text
db/schema/questdb_pr34_add_phase7_context_ledger.sql
```

The migration is additive and idempotent. Back up first, record legacy table
row counts and columns, run the migration in file order, rerun it, confirm the
legacy counts are unchanged, and confirm both new tables exist with zero rows
before restarting writers. Partial application is recovered by rerunning this
migration, never by executing the destructive reset. See
`docs/live_runbook.md` and `docs/questdb_schema.md`.

During PR34 preflight, the running local QuestDB was inspected read-only. Its
`context_ai_events` and `context_flags` columns matched the pre-PR34 committed
schema and both tables contained zero rows at that moment. No live mutation was
performed.

## Validation

Use only the repository interpreter:

```powershell
& ".\.venv\Scripts\python.exe" scripts/check_config.py
& ".\.venv\Scripts\python.exe" scripts/check_contracts.py
& ".\.venv\Scripts\python.exe" scripts/check_questdb_schema.py
& ".\.venv\Scripts\python.exe" -m pytest tests/unit/test_fred_collector.py
& ".\.venv\Scripts\python.exe" -m pytest tests/unit/test_contracts_context.py
& ".\.venv\Scripts\python.exe" -m pytest tests/unit/test_questdb_writer.py
& ".\.venv\Scripts\python.exe" -m pytest tests/unit/test_decision_context.py
& ".\.venv\Scripts\python.exe" -m pytest
git diff --check
```

PR34 validation must stay offline. Do not run live context-source, broker,
Gemini, SEC, or QuestDB write tests for this PR.

## Explicit follow-ups

- PR35: trusted-source registry, timestamp/hash enforcement, cross-record
  provenance, as-of validation, abstention/rejection policy, and prompt-injection
  safety.
- PR36: Gemini provider and bounded queue, timeouts/retries/rates/budgets,
  deduplication/backpressure, plus correlated retained local full-exception logs.
- PR37: research cache, as-of selection, and real shadow-policy evaluator that
  never changes the real risk result.
- PR38: SEC EDGAR collector, immutable local archive, bounded 8-K sections, and
  deterministic Form 4 P/S parsing.
- PR39: provider-neutral manual news/social inbox ingestion through the same
  validation and research-only pipeline.
