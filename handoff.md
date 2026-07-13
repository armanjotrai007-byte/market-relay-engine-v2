# Market Relay Engine V2 Handoff

## Current work

Repository: `armanjotrai007-byte/market-relay-engine-v2`

PR35 review branch:

```text
pr35-live-gemini-context-filter
```

Base `main` SHA:

```text
ea55725416e77b3503f99eca4e9bfba28af36f04
```

PR35 builds the reusable live Gemini Interactions classifier on the merged PR34
contracts. It adds a versioned prompt, contract-derived JSON Schema, explicit
retry ownership and attempt accounting, bounded process-local deduplication,
local provider-call budgets, offline tests, and one explicitly gated live
checker. It does not add SEC/news/social collection, persistent caching,
QuestDB writes, risk integration, or broker/execution behavior.

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

Gemini-classification event types retain the bounded SEC 8-K values and add
general government-contract, regulatory-policy, geopolitical,
supply-disruption, earnings-guidance, legal, cybersecurity,
management-change, and social/political categories. `UNKNOWN` remains reserved
for non-valid response shapes. Form 4 purchase/sale values remain deterministic
and outside Gemini.

`available_at` means the earliest trusted demonstrable public availability of
the underlying fact. When both top-level `ContextFlag.available_at` and legacy
`details["provenance"]["available_at"]` exist, adapters require equal UTC
instants. EIA preserves its existing pre-release risk window while placing the
official release time in both availability representations.

Only a safe provider failure category/summary may enter contracts and any later
caller-owned QuestDB write. PR35 does not retain or emit raw provider
exceptions, prompts, source text, headers, or credentials.

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

Then apply the PR35 accounting extension before a later caller writes live
classification attempts:

```text
db/schema/questdb_pr35_add_context_classification_accounting.sql
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
& ".\.venv\Scripts\python.exe" scripts/check_gemini_context.py --help
& ".\.venv\Scripts\python.exe" scripts/check_gemini_context.py
& ".\.venv\Scripts\python.exe" -m pytest tests/unit/test_fred_collector.py
& ".\.venv\Scripts\python.exe" -m pytest tests/unit/test_contracts_context.py
& ".\.venv\Scripts\python.exe" -m pytest tests/unit/test_questdb_writer.py
& ".\.venv\Scripts\python.exe" -m pytest tests/unit/test_decision_context.py
& ".\.venv\Scripts\python.exe" -m pytest
git diff --check
```

Default PR35 validation stays offline. After it passes, run exactly one explicit
`scripts/check_gemini_context.py --live --required` acceptance check. Do not run
live source collectors, broker actions, or QuestDB writes.

## Explicit follow-ups

- PR36+: trusted-source enforcement, source collectors, bounded orchestration,
  and integration with the later persistent research cache.
- PR37: research cache, as-of selection, and real shadow-policy evaluator that
  never changes the real risk result.
- PR38: SEC EDGAR collector, immutable local archive, bounded 8-K sections, and
  deterministic Form 4 P/S parsing.
- PR39: provider-neutral manual news/social inbox ingestion through the same
  validation and research-only pipeline.
