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
trusted-code-created raw-input metadata
-> normalized source-document metadata and hashes
-> bounded in-memory classification request where AI is needed
-> versioned source-neutral prompt and Gemini Interactions API
-> strict schema-constrained classification response and validation
-> research-only ContextAIEvent / ContextFlag
-> future research cache
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
without repeating Gemini. A broader persistent research cache, as-of selection,
and shadow-policy evaluation remain later work.

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
abstained responses are cached. Persistence across restarts belongs to the
later research-cache integration. Per-minute and per-run call counters prevent
accidental runaway use but do not replace Google project quotas or billing
controls.

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
