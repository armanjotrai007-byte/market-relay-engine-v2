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

`ContextRawInput` identifies a trusted-code ingress envelope by source,
source type, source locator, affected tickers, collection time, and raw-input
hash. `ContextSourceDocument` adds normalized document identity, document hash,
and normalization time. Neither contract contains source body text.

`ContextClassificationRequest` is the only Phase 7 contract allowed to carry a
bounded in-memory excerpt in `input_text`. It also carries deterministic source
identity, hashes, ticker mappings, source timestamps, request time, and prompt
version. It is not a durable raw-document record, and the excerpt must never be
written to QuestDB or emergency ledger rows. PR35 owns the input/output bounds,
provider call, retry, local call budgets, and bounded process-local
deduplication. Persistent caching, queues, and broader pipeline backpressure
remain deferred.

`ContextClassificationResponse` records one logical classification attempt without granting
the provider authority to invent source identity, tickers, hashes, timestamps,
or risk policy. `ContextValidationResult` records the validator outcome,
machine-readable reason codes, safe detail, validator version, and validation
time. Trusted-source enforcement, hash recomputation, prompt-injection checks,
and cross-record provenance validation belong to PR35.

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

Form 4 open-market purchase and sale values are deliberately separate:

```text
SEC_FORM4_OPEN_MARKET_PURCHASE
SEC_FORM4_OPEN_MARKET_SALE
```

They belong to `DeterministicContextEventType`, not the AI classification enum.
`ContextClassificationResponse` and AI-derived `ContextAIEvent` reject those
values. Deterministic Form 4 parsing and event emission remain deferred to PR38.

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
risk adapters remain compatible. Phase 7 metadata is optional until PR35
enforces complete trusted lineage.

`available_at` means the earliest trusted, demonstrable time the underlying
fact was publicly available. It is not the local collection time, the source
event time, or the start of a pre-release risk window. When a `ContextFlag` and
its legacy `details["provenance"]` both represent availability, their values
must agree exactly after UTC normalization; malformed values or mismatches are
rejected. EIA continues to use its reviewed official release time in both
locations.

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
hypothetical output. PR37 owns the research cache, as-of selection, and actual
shadow evaluator and must never alter the real risk decision.

## Timestamp meanings

- `source_published_at` / `source_updated_at`: source-declared document times.
- `collected_at`: when trusted local code accepted the raw input.
- `normalized_at`: when source document metadata was normalized.
- `requested_at`: when a classification request was created.
- `classified_at`: when one classification attempt completed.
- `available_at`: earliest trusted demonstrable public availability.
- `validated_at`: when validation completed.
- `event_time`: canonical time of an event or flag record.
- `valid_from` / `valid_until`: event or policy window bounds; they are not
  proof that the underlying fact was publicly available.
- `decision_evaluation_time`: the explicit as-of time of a shadow comparison.
- `write_time`: local ledger write-attempt time.

## Serialization and validation

`market_relay_engine.common.serialization` converts dataclasses, enums,
datetimes, lists, and dictionaries to JSON-safe values. It does not reconstruct
dataclass instances from untrusted JSON.

Run contract checks offline with the repository virtual environment:

```powershell
& ".\.venv\Scripts\python.exe" scripts/check_contracts.py
& ".\.venv\Scripts\python.exe" -m pytest tests/unit/test_contracts_context.py
```

PR35 adds live Gemini classification only. SEC collection, news/social
collection, archive writing, manual inbox processing, persistent research-cache
behavior, real shadow-policy execution, and real risk integration remain
deferred.
