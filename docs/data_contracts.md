# Data Contracts

Market Relay Engine V2 uses frozen standard-library dataclasses, string-backed
enums, generated record IDs, and explicit UTC timestamps. The contracts are
transport and audit shapes; they are not an ORM, provider client, source
registry, or trading policy.

## Common rules

- Datetimes must be timezone-aware. Aware offsets are normalized to UTC, naive
  values are rejected, and JSON serialization emits UTC ISO strings ending in
  `Z`.
- Required identities and text fields are non-empty strings.
- SHA-256 references are 64-character lowercase hexadecimal strings. PR34
  validates their representation but does not read files or recompute hashes.
- Mutable input collections are defensively copied.
- Confidence is finite and in the inclusive range `[0, 1]`.
- Enums serialize to their stable string values.
- `schema_version` and `trace_id` travel with records for evolution and
  correlation. Record IDs are generated when callers do not supply them.

Existing market, feature, signal, risk, context-state, execution, outcome,
latency, and system-health contracts keep their established behavior. PR34 does
not change `RiskDecision`, model inference, sizing, broker, or execution logic.

## Phase 7 source lineage

Phase 7 uses a provider-neutral lineage:

```text
ContextRawInput.raw_input_id + raw_input_hash
-> ContextSourceDocument.source_document_id + document_hash
-> ContextClassificationRequest.classification_request_id
-> ContextClassificationResponse.classification_attempt_id
-> ContextValidationResult.validation_result_id
-> research-only ContextAIEvent / ContextFlag
```

The external pilot adds backward-compatible lifecycle and readiness metadata to
the same contracts rather than defining a competing source-document or event
contract. A revisable record can carry `source_fact_id`,
`source_revision_id`, `revision_sequence`, `supersedes_revision_id`,
`lifecycle_state`, and `lifecycle_effective_at`. VeritaWire uses the underlying
Truth Social post ID as `source_fact_id`. Immutable revisions preserve their
own bytes, normalized text, excerpt, classification result, and timestamps;
edits and deletion/retraction notices never overwrite an earlier revision.

`ContextRawInput` identifies a trusted-code ingress envelope by source,
source type, source locator, affected tickers, collection time, and raw-input
hash. `ContextSourceDocument` adds normalized document identity, document hash,
and normalization time. Neither contract contains source body text.

`ContextClassificationRequest` is the only Phase 7 contract allowed to carry a
bounded in-memory excerpt in `input_text`. It also carries deterministic source
identity, hashes, trusted ticker/sector/global scope, source timestamps, request
time, and prompt version. It is not a durable raw-document record, and the
excerpt must never be written to QuestDB or emergency ledger rows. PR35 owns
the input/output bounds, provider call, retry, local call budgets, and bounded
process-local deduplication. The external archive now owns persistent completed
classification suppression across restarts; pending budget/provider failures
remain retryable and do not become permanent irrelevance.

`ContextClassificationResponse` records one logical classification attempt without granting
the provider authority to invent source identity, hashes, timestamps, or risk
policy. Response schema v2 may return strict `affected_tickers`,
`affected_sectors`, and `global_relevance`, but tickers and sectors must come
from configured allowlists. Trusted deterministic ticker matches are unioned
with valid provider scope, so AI cannot remove an explicitly observed approved
ticker. `ContextValidationResult` records the validator outcome,
machine-readable reason codes, safe detail, validator version, and validation
time. Trusted-source enforcement, hash recomputation, prompt-injection checks,
scope allowlists, and cross-record provenance validation belong to PR35.

## Classification vocabulary

`ContextClassificationEventType` limits Gemini-classifiable event values to:

```text
UNKNOWN
OTHER
GOVERNMENT_CONTRACT
REGULATORY_POLICY
GEOPOLITICAL
SUPPLY_DISRUPTION
EARNINGS_GUIDANCE
LEGAL
CYBERSECURITY
MANAGEMENT_CHANGE
SOCIAL_POLITICAL_STATEMENT
SEC_8K_MATERIAL_AGREEMENT
SEC_8K_TERMINATION_OF_MATERIAL_AGREEMENT
SEC_8K_BANKRUPTCY
SEC_8K_CYBERSECURITY_INCIDENT
SEC_8K_ACQUISITION
SEC_8K_RESULTS
SEC_8K_DIRECT_FINANCIAL_OBLIGATION
SEC_8K_DEBT_DEFAULT
SEC_8K_EXIT_OR_DISPOSAL_COSTS
SEC_8K_MATERIAL_IMPAIRMENT
SEC_8K_DELISTING
SEC_8K_AUDITOR_CHANGE
SEC_8K_NON_RELIANCE
SEC_8K_CHANGE_IN_CONTROL
SEC_8K_EXECUTIVE_OR_DIRECTOR_CHANGE
SEC_8K_REGULATION_FD
SEC_8K_OTHER_EVENT
```

Risk levels are `UNKNOWN`, `LOW`, `MEDIUM`, `HIGH`, and `CRITICAL`. Urgency is
`UNKNOWN`, `LOW`, `MEDIUM`, or `HIGH`.

Form 4 purchase and sale values are deliberately separate and venue-neutral:

```text
SEC_FORM4_PURCHASE
SEC_FORM4_SALE
```

They belong to `DeterministicContextEventType`, not the AI classification enum.
`ContextClassificationResponse` and AI-derived `ContextAIEvent` reject those
values. The SEC collector parses non-derivative Form 4 P/S facts
deterministically into local research events. SEC transaction codes P and S
cover open-market or private transactions, so PR36 does not infer a venue from
the code alone. These events remain outside the AI contract and real
risk/execution paths.

## Classification statuses

`ContextClassificationStatus` has exactly four values:

- `VALID`: non-unknown event type, risk level, urgency, confidence, and concise
  summary are present; provider-failure fields are absent.
- `ABSTAINED`: classification fields remain unknown, confidence is absent, and
  an optional safe summary may explain the abstention.
- `VALIDATION_REJECTED`: classification and provider-failure payloads are
  absent; rejection reasons are recorded by `ContextValidationResult`.
- `PROVIDER_FAILED`: classification payload is absent and a safe failure
  category is required; a safe summary is optional.

Only the safe failure category and summary may enter contracts, QuestDB, or the
emergency ledger. Full exceptions, raw responses, headers, source text,
rendered prompts, and tracebacks are excluded; credentials are always redacted.

## PR35 classification-attempt accounting

One `classify()` invocation creates one logical classification attempt and maps
to one `context_classification_attempts` ledger row when a caller later writes
it. Internal HTTP retries never create additional attempt rows. The response
records `provider_request_count`, `retry_count`, `deduplicated`, and optional
`reused_classification_attempt_id` with backward-compatible defaults.

- First-call success is one provider request and zero retries.
- Success or terminal provider failure after two retries is three provider
  requests and two retries.
- A local pre-network failure has zero provider requests and zero retries.
- A deduplication hit has zero provider requests, zero retries,
  `deduplicated=true`, and references the original cached attempt.

Only `VALID` and `ABSTAINED` results enter the bounded process-local cache.
`PROVIDER_FAILED` and `VALIDATION_REJECTED` results do not. PR35 updates the
ledger schema and writer mapping for compatibility but neither the classifier
nor its checker writes to a live QuestDB.

## Research-only events and flags

`ContextAIEvent` and `ContextFlag` remain the single canonical event and flag
contracts; PR34 evolves them rather than creating competing copies. They can
carry source/document/request/attempt/validation lineage, hashes, affected
tickers, availability, provider metadata, and concise structured output.

`ContextAIEvent` uses only the AI-classifiable event enum. `ContextFlag` keeps
its legacy generic flag and severity fields so existing EIA and deterministic
risk adapters remain compatible. External VeritaWire, company-news, and
earnings classifications normally materialize one `ContextAIEvent`; they are
not duplicated into `ContextFlag` by default. The additive plural
`affected_sectors` and explicit `global_relevance` fields coexist with legacy
`affected_sector`. Tickers, all effective sectors, and global relevance are
simultaneous scopes, and decision matching is:

```text
global_relevance
OR decision ticker in affected_tickers
OR decision sector in affected_sectors
```

An event may truthfully satisfy all three clauses. Collections are uppercase,
sorted, deduplicated, and bounded by the approved universe; a second applicable
sector is not silently discarded.

Legacy `available_at` retains its established meaning. For the new external
`ContextAIEvent` path, the returned, ledgered, and hydrated event's
`available_at` mirrors its per-revision durable `evidence_ready_at`, while
`source_available_at` preserves the separate public/source time. The immutable
pre-readiness event payload leaves both readiness fields null; its separately
published readiness receipt is authoritative. External records also preserve
`system_observed_at` and `archived_at` so source publication and
derived-evidence readiness cannot be confused. When a
`ContextFlag` and its legacy
`details["provenance"]` both represent availability, their values must agree
exactly after UTC normalization; malformed values or mismatches are rejected.
EIA continues to use its reviewed official release time in both locations.

These records are research-only in PR34. They do not enter
`approved_risk_context`, approve or block a real trade, resize or delay a real
trade, or modify a `RiskDecision`.

## Shadow policy evaluations

`ShadowContextPolicyEvaluation` joins a model signal and optional risk decision
to matched context event/flag IDs at one explicit
`decision_evaluation_time`. It records a deterministic context fingerprint,
policy version, policy-config hash, reason codes, and one hypothetical action:

```text
NO_CHANGE
BLOCK
REDUCE_SIZE
DELAY
WARN_ONLY
```

`proposed_size_factor` is required only for `REDUCE_SIZE` and must satisfy
`0 < factor <= 1`; every other action rejects a size factor. This is audit-only
hypothetical output. PR37 supplies deterministic in-memory as-of selection and
the shadow evaluator without adding another public event or evaluation
contract.  It adapts PR36's existing `Form4ResearchEvent` facts internally and
must never alter the real risk decision.

## Timestamp meanings

- `source_published_at` / `source_updated_at`: source-declared document times.
- `source_available_at`: earliest trusted source/public availability retained
  for explicit historical-source-time research.
- `system_observed_at`: when this collector actually received or fetched the
  immutable source revision.
- `collected_at`: when trusted local code accepted the raw input.
- `archived_at`: when raw bytes and immutable revision metadata were durably
  published.
- `normalized_at`: when source document metadata was normalized.
- `requested_at`: when a classification request was created.
- `classified_at`: when one classification attempt completed.
- `available_at`: legacy compatibility availability; for new external
  `ContextAIEvent` records it mirrors durable `evidence_ready_at`.
- `validated_at`: when validation completed.
- `evidence_ready_at`: no earlier than observation, archive/normalization,
  classification, validation, canonical durable publication, and readiness
  publication; live-system selection cannot precede it.
- `event_time`: canonical time of an event or flag record.
- `valid_from` / `valid_until`: event or policy window bounds; they are not
  proof that the underlying fact was publicly available.
- `decision_evaluation_time`: the explicit as-of time of a shadow comparison.
- `write_time`: local ledger write-attempt time.

## Internal research preparation contracts

The public source/classification contracts above normalize into PR37's internal
`ResearchEvidence`; no second public event contract is introduced.
`ResearchRunDefinition` pins one legacy SEC profile plus any exact external
`ResearchSourceClassificationProfile` values. External ownership includes
source, source type, optional fixed ticker, semantic adapter, extractor,
normalizer, excerpt, scope, prompt, model, response schema, validator, and
classifier configuration. This permits separate PLTR and LMT earnings profiles
and multiple reviewed extractor variants without selecting two profiles for one
revision.

`ResearchSourceCoverageProfile` pins coverage by source, fixed ticker when
applicable, and semantic adapter, together with its manifest identity,
generation, and version. `ResearchAvailabilityMode` is either
`LIVE_SYSTEM_READY` or `HISTORICAL_SOURCE_TIME`; external evidence cannot be
prepared with the mode unset. Lifecycle revisions are resolved before exact
duplicates. `ResearchEvidenceRelationship` records deterministic linkage such
as a related SEC/company earnings occurrence without merging unequal text or
transferring availability.

The canonical classification-input fingerprint covers document, normalized,
and excerpt hashes, trusted input scope, and the exact pinned semantic profile;
it excludes generated output, IDs, timestamps, and latency. Separate complete-
and policy-output fingerprints expose contradictory results. Unresolved
conflicts fail closed. Reviewed resolution artifacts and their manifest
generation are pinned by the run, and every selection-changing profile,
coverage, availability, lifecycle, correlation, archive, and resolution setting
enters the research fingerprint.

`ResearchEvidence.canonical_classification_owner_fingerprint` is nullable and
internal. Combined preparation assigns a shared value across SEC/company
observations only when exact document, normalized-text, excerpt, and durable
trusted-input-scope identity match. It does not replace either source-native
`classification_input_fingerprint`; missing legacy trusted scope leaves the
source-specific owner in place. The owner affects new research fingerprints,
while its absence preserves unchanged legacy SEC-only payloads and hashes.

## Serialization and validation

`market_relay_engine.common.serialization` converts dataclasses, enums,
datetimes, lists, and dictionaries to JSON-safe values. It does not reconstruct
dataclass instances from untrusted JSON.

Run contract checks offline with the repository virtual environment:

```powershell
& ".\.venv\Scripts\python.exe" scripts/check_contracts.py
& ".\.venv\Scripts\python.exe" -m pytest tests/unit/test_contracts_context.py
```

PR35 supplies the shared Gemini boundary; PR36 supplies SEC collection; the
external pilot now supplies bounded news/social and earnings collection plus
durable classification ownership. It still adds no raw manual inbox, generic
AI database, real policy execution, or real risk integration.
