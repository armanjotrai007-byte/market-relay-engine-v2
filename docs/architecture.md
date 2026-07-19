# Architecture

Market Relay Engine V2 is a local, testable trading research and future paper/live execution system with two deliberately separate context paths.

```text
Official Databento historical Parquet/DBN
-> normalized MarketRecord
-> canonical feature builder
-> supervised training and backtesting

Databento live DBN
-> normalized MarketRecord
-> same canonical feature builder
-> calibrated model signal
-> deterministic Python risk gate
-> Alpaca paper execution
```

The current structured collectors update an in-memory `ContextStateCache` outside the per-tick loop. `DecisionContextAssembler` projects visible structured evidence for research and audit, but does not call collectors, read QuestDB, or change a `RiskDecision`.

Phase 7 adds a separate research-only path:

```text
official SEC / VeritaWire / LMT / PLTR / earnings connector
-> immutable content-addressed source archive and lifecycle revision
-> trusted-code-created raw-input metadata
-> normalized source-document metadata and hashes
-> deterministic scope-aware bounded classification request
-> versioned source-neutral prompt and Gemini Interactions API
-> strict schema-constrained classification response and validation
-> durable canonical classification claim and research-only ContextAIEvent
-> explicit combined archive hydration into one bounded in-memory event index
-> existing structured DecisionContext + leak-free event selection
-> hypothetical ShadowContextPolicyEvaluation
```

PR34 defines the contracts and ledger metadata. PR35 implements the reusable
Gemini classification boundary. PR36 adds a source-specific, explicitly
invoked SEC EDGAR research collector for the approved ten issuers. It archives
original SEC documents and complete normalized 8-K items immutably, sends only
versioned bounded excerpts to the existing classifier, and parses official Form
4 XML directly. Its local SEC manifest stores safe successful results before
optional ledger writes and suppresses repeat paid calls after restart. PR35's
LRU remains same-process protection only.

Form 4 normalization preserves derivative and non-derivative transactions, but
only non-derivative P/S records become initial research events. Unresolved Form
4/A events remain research-accessible and are excluded from default aggregates.
They retain a separate deterministic vocabulary and are never Gemini values.

The SEC path does not populate `approved_risk_context`, update `ContextState`,
or alter risk, model features, orders, positions, Alpaca, or execution. QuestDB
receives safe classification-attempt metadata only, never filings, sections,
prompts, or provider bodies. QuestDB failure uses the existing JSONL fallback
without repeating Gemini.

PR37 adds no persistent generic research cache.  An explicit preparation step
reads durable PR36 SEC output and the external-event archive, pins exact
source-specific classification and coverage profiles, validates ownership, and
atomically hydrates one bounded in-memory event index. The pilot sources are
VeritaWire-delivered Truth Social posts, Lockheed Martin's official all-news
RSS and linked articles, Palantir investor-relations releases, and official
PLTR/LMT earnings releases. Signal-time selection then combines that event view
with the unchanged structured `DecisionContext.context_fingerprint` without
filesystem, QuestDB, network, SEC, Gemini, or broker reads. The resulting
policy action remains hypothetical and defaults to `NO_CHANGE`.

Every revisable source fact keeps immutable lifecycle revisions. Preparation
resolves the current revision as of the decision time before exact-duplicate
collapse. An observed edit suppresses the prior revision immediately; if the
edit is not evidence-ready, selection does not fall back to old text. A current
`DELETED` or `RETRACTED` revision suppresses all prior content, and ambiguous
revision order fails closed. Only after lifecycle resolution and canonical
classification-conflict checks may identical meaningful inputs collapse into
one policy-active fact with as-of-visible observation lineage. Different text
or classification input remains separate evidence; deterministic correlation
is relationship metadata only and never transfers content or an earlier
availability timestamp.

External timestamps have separate meanings. `source_available_at` preserves
the earliest trusted source/public time, `system_observed_at` records this
collector's receipt, and `evidence_ready_at` is no earlier than observation,
archive/normalization, classification, validation, canonical durable
publication, and readiness publication. `LIVE_SYSTEM_READY`, the default mode
for the pilot, selects by `evidence_ready_at`. The explicit counterfactual
`HISTORICAL_SOURCE_TIME` mode selects by source availability only after
preparation proves complete coverage for the requested range. A run cannot mix
the modes, and the selected mode enters its fingerprint.

External scope is a union, not a precedence choice. One source fact may carry
multiple approved tickers, multiple reviewed sectors, and
`global_relevance=true` simultaneously. Selection uses OR semantics. Trusted
fixed company tickers and deterministic explicit aliases are retained; strict
v2 classifier scope may add only allowlisted tickers/sectors and cannot remove
a directly observed approved ticker. Multi-scope evidence remains one source
fact rather than being copied into ticker-owned duplicates.

The live implementation uses `client.interactions.create` with the configured
model and a rendered bounded prompt. Its `response_format` selects text with
`application/json` and an explicit contract-derived schema. Every request sets
`store=False`; it supplies no previous interaction ID, tools, browsing, code
execution, agents, background job, or server-side conversation history. The
bounded source text is explicitly marked as untrusted data, while Python owns
source identity, hashes, URLs, timestamps, ticker/sector mappings, and response
lineage. Provider output cannot replace that metadata.

SDK retries are disabled at one total HTTP attempt. A repository-owned loop may
retry timeout/network, 429/resource exhaustion, and retryable 5xx failures up
to two times, so one logical classification makes at most three provider calls.
Authentication, permission, safety, malformed/schema-invalid output,
deterministic contract errors, and local-budget exhaustion are not retried.

A bounded process-local LRU cache deduplicates identical trusted content and
configuration. Its fingerprint covers raw/document hashes, source document ID,
affected tickers, source type, prompt version, model, and response-schema
version; source text is never an unbounded dictionary key. Only valid and
abstained responses are cached. For the external pilot, the immutable archive
also supplies restart-safe suppression: the first validated durable result
atomically owns a canonical classification-input fingerprint covering the
semantic request and exact source profile. Generated output, IDs, timestamps,
and latency are excluded from that input identity. Later processes and
backfills reuse the claim without another Gemini call. Per-minute and per-run
call counters still prevent accidental runaway use but do not replace Google
project quotas or billing controls.

External preparation pins the archive manifest and conflict-resolution
manifest generations and hashes, then verifies the archive pin again before
publishing the index. Complete- and policy-output fingerprints detect
contradictory results under one canonical input. An unresolved conflict blocks
preparation. Reviewed immutable resolutions may keep the chronologically proven
first live result, abstain the input, or require reclassification under a new
profile; the run must pin the applicable resolution generation and profile.
Coverage is owned by source/ticker/adapter, records live/backfill ranges and
known gaps, and fails closed by default when the requested interval is not
complete. An explicit incomplete-coverage override is audit-visible and changes
the research fingerprint.

Provider failures expose only a fixed safe category and concise safe summary to
contracts, logs, and any later caller-owned QuestDB write. Raw exception text,
tracebacks, responses, prompts, source text, headers, and credentials are not
retained or emitted.

QuestDB is the bot ledger and black-box recorder. It stores IDs, hashes, concise summaries, validation metadata, shadow results, signals, risk decisions, orders, fills, latency, slippage, PnL, outcomes, and health. It is not a historical market-data warehouse and must not store full filings, articles, posts, normalized documents, request excerpts, prompts, credentials, or secrets.

The classifier writes no QuestDB rows itself. One caller-owned ledger row maps
to one logical `classify()` call; its internal HTTP retries are counts on that
same attempt. The deterministic Python risk gate remains the only final
pre-trade authority. AI and external context have no direct trade, block,
delay, order, broker, execution, or sizing authority.
