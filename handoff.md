# Market Relay Engine V2 Handoff

## Current work

Repository: `armanjotrai007-byte/market-relay-engine-v2`

PR37 implementation branch:

```text
pr37-context-shadow-evaluation
```

Base `main` SHA:

```text
f0d0753267ef07bba1454d5c41303f2dc2c17905
```

PR37 consumes the existing structured `DecisionContext` and PR36's durable SEC
outputs to produce leak-free, research-only shadow policy evaluations.  It pins
one 8-K classification profile, hydrates a bounded in-memory event index before
signal evaluation, applies an explicit finite lookback where native expiry is
absent, and writes only the existing `ShadowContextPolicyEvaluation` shape.
The default policy returns `NO_CHANGE`; no real risk, model, execution, or
broker behavior changes.

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
configuration. SEC collection is invoked explicitly and is research-only. SEC
requests are sequential, default to 2/second, reject configuration above
8/second, honor bounded 429 `Retry-After`, back off boundedly for retryable 5xx,
and stop on a potential fair-access 403.

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
-> explicit bounded in-memory research-event hydration
-> leak-free selection at model-signal time
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
caller-owned QuestDB write. PR35/PR36 do not retain or emit raw provider
exceptions, prompts, source text, headers, or credentials.

The SEC manifest, not a generated contract ID, owns restart-safe identity. Its
classification key includes accession, official document, item, full-section
and excerpt hashes, extraction/prompt/model/schema versions, and relevant
classification configuration. PR35's LRU remains same-process protection.
`VALID`/`ABSTAINED` results are saved completely and atomically before QuestDB;
fallback or pending ledger state can be retried without another Gemini call.

Official Form 4 XML is normalized for derivative and non-derivative
transactions. Only non-derivative P/S values are promoted. Unresolved Form 4/A
records remain archived with `AMENDMENT_UNRESOLVED` and are excluded from
default aggregate counts; PR36 does not guess amendment relationships.

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
& ".\.venv\Scripts\python.exe" scripts/check_sec_edgar.py
& ".\.venv\Scripts\python.exe" scripts/check_context_shadow_evaluation.py
& ".\.venv\Scripts\python.exe" -m pytest tests/unit/test_research_projection.py
& ".\.venv\Scripts\python.exe" -m pytest tests/unit/test_sec_edgar.py
& ".\.venv\Scripts\python.exe" -m pytest tests/unit/test_fred_collector.py
& ".\.venv\Scripts\python.exe" -m pytest tests/unit/test_contracts_context.py
& ".\.venv\Scripts\python.exe" -m pytest tests/unit/test_questdb_writer.py
& ".\.venv\Scripts\python.exe" -m pytest tests/unit/test_decision_context.py
& ".\.venv\Scripts\python.exe" -m pytest
git diff --check
```

Default PR37 validation stays offline. The manually gated SEC read-only smoke
check is:

```powershell
& ".\.venv\Scripts\python.exe" scripts/check_sec_edgar.py --live --ticker LMT --form 8-K --max-filings 1
```

It has no broker, Gemini, or QuestDB action. Do not use `--classify` or
`--questdb` without explicitly intending those external calls.

## Explicit follow-ups

- Later research must join shadow evaluations to actual after-cost outcomes and
  test policy versions before any claim that context improves profitability.
- PR39: provider-neutral manual news/social inbox ingestion through the same
  validation and research-only pipeline; only resulting validated
  `ContextAIEvent`/event-owned `ContextFlag` records may reuse PR37's seam.
