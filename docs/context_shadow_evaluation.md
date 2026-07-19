# Research Context Shadow Evaluation

PR37 joins the existing structured `DecisionContext` with research-only event
evidence at one model-signal time.  The result is a hypothetical
`ShadowContextPolicyEvaluation`; it has no authority over the model, real risk
decision, position size, order, or broker.

## Evidence ownership

Each source fact has exactly one owner.

- EIA, FRED, USAspending, the reviewed macro calendar, yfinance development
  context, and their structured flags remain in `ContextStateCache` and are
  projected only by `DecisionContextAssembler`.
- Valid SEC 8-K classifications and deterministic Form 4 purchase/sale facts
  use the research-event path.
- VeritaWire Truth Social, Lockheed Martin RSS/article, Palantir IR, and
  official PLTR/LMT earnings revisions use this path only after archive-first
  collection, the existing classification/validation pipeline, and durable
  publication as a validated `ContextAIEvent`. External multi-scope news is not
  duplicated into ticker-owned flags by default.

The research-run definition declares admitted event sources and evidence
categories. Each normalized event fact is owned by its canonical source fact
and immutable revision identity. Every admitted external source and every
in-range revision must resolve to exactly one pinned source/ticker/extractor
profile; missing or ambiguous ownership stops hydration. Any source appearing
in both event evidence and the supplied `DecisionContext` also aborts
evaluation. Structured entries are never copied or silently deduplicated into
the event index.

## Preparation and in-memory evaluation

Archive I/O is a preparation step, not a signal-time operation:

```text
explicit research-run definition
-> read durable PR36 SEC and external-event records
-> verify archive, classification, conflict-resolution, and coverage pins
-> validate source/ticker/profile ownership and readiness chronology
-> normalize safe evidence metadata
-> resolve deterministic relationships without merging content
-> atomically hydrate a bounded in-memory index
-> freeze the index
-> evaluate model signals from memory only
```

The run definition explicitly supplies its ticker universe, admitted sources
and categories, hydration time range, exact classification profiles, finite
maximum age for evidence without native expiry, availability mode, source
coverage profiles, archive/conflict-resolution generations and hashes, and
capacity. Hydration never scans unbounded history by default. Its start must be
at least one explicit lookback before the earliest signal the run will
evaluate. After hydration, selection and evaluation perform no filesystem,
QuestDB, network, SEC, Gemini, or broker access.

Capacity overflow aborts hydration before an index is published.  The failure
reports the attempted count, capacity, universe, and window.  The caller must
narrow the universe, narrow the time range, or explicitly raise capacity; no
evidence is silently evicted and no partial evaluation is permitted.

## SEC classification profile

Every run pins one 8-K classification profile: extraction version, prompt
version, model version, response-schema version, and classification
configuration hash.  Only a validated `VALID` result matching that exact
profile is eligible for its source section.  Other profiles remain auditable
as exclusions and never become simultaneous evidence.  There is no implicit
"latest wins" behavior.

If the profile is missing, hydration stops.  If more than one result under the
same profile claims the same accession, official document, item, document hash,
and full-section hash, hydration stops as a source-section conflict.

Form 4 remains deterministic.  PR37 adapts PR36's existing
`Form4ResearchEvent` archive facts without treating them as Gemini output.
Its document lineage uses PR36's selected `official_document_url`, not the
filing-discovery URL.
Unresolved amendment events remain available for audit but are policy
ineligible unless their PR36 aggregate eligibility is `ELIGIBLE`.

## External profiles, ownership, and coverage

An external `ResearchSourceClassificationProfile` pins source, source type,
optional fixed ticker, semantic adapter, extractor, normalizer, excerpt, scope,
prompt, model, response schema, validator, and classifier configuration. The
revision's trusted ticker and adapter/extractor/normalizer versions must select
exactly one profile. This supports PLTR and LMT earnings ownership plus reviewed
HTML, PDF, and plain-text extractor variants without any `latest wins`
behavior or simultaneous classifications of one revision under multiple
profiles.

Coverage is separately owned by source, fixed ticker when applicable, and
semantic adapter. Its manifest records status, covered ranges, live-collection
start, bounded backfills, known gaps, generation, and version. Statuses include
`LIVE_ONLY`, `PARTIAL`, `COMPLETE_FOR_RANGE`, and `UNKNOWN`. Preparation fails
when the requested interval is not completely covered. A live-system run may
explicitly set `allow_incomplete_coverage`, but that setting and the incomplete
assessment enter result metadata and the fingerprint. Historical-source-time
runs cannot use that override.

The run pins the external archive manifest generation/hash and the immutable
conflict-resolution manifest generation/hash. Classification attempts,
canonical claims, materialized events, readiness receipts, and coverage
artifacts are registered in the archive manifest. Preparation verifies the pin
before reading and again before index publication, so a run definition cannot
hydrate changed mutable state under an old fingerprint.

## Canonical classification ownership and conflict resolution

The first successfully validated and durably published result atomically owns
its `classification_input_fingerprint`. That identity covers the canonical
semantic request and exact profile, including document/normalized/excerpt
hashes and trusted input scope. It excludes generated output, record IDs,
timestamps, and latency. Later live processes and backfills reuse the canonical
result without another Gemini call.

`complete_output_fingerprint` and `policy_output_fingerprint` make
contradictory outputs under one canonical input detectable. An unresolved
`CLASSIFICATION_CONFLICT` blocks preparation; it is never resolved by latest,
majority, confidence, severity, merging, or another call under the same
profile. A reviewed immutable resolution may:

- `KEEP_FIRST_DURABLY_PUBLISHED` only when archive chronology proves the first
  live result was canonical before a contradictory backfill, with both its
  complete-output and policy-output fingerprints pinned;
- `ABSTAIN_INPUT`, which admits neither result; or
- `RECLASSIFY_UNDER_NEW_PROFILE`, which requires the run to pin that reviewed
  new profile.

Resolution identity, both chosen output hashes, and the pinned manifest
generation remain fingerprinted audit state. Old attempts are never
overwritten.

Immutable attempts, canonical claims, materialized events, readiness receipts,
and reviewed resolutions are written before their mutable manifest entries.
Restart reconciliation validates embedded identities, timestamps, output and
profile hashes before adopting a complete orphan. Preparation reconciles before
checking pinned generations, so recovery fails an older run pin closed.

## Leak-free selection

External runs choose exactly one availability mode:

- `LIVE_SYSTEM_READY` selects by `evidence_ready_at`. This timestamp is no
  earlier than source observation, archive/normalization, classification,
  validation, canonical durable publication, and readiness artifact
  publication. A merely archived or budget-delayed record remains pending.
- `HISTORICAL_SOURCE_TIME` is an explicit counterfactual selecting by
  `source_available_at`; it is permitted only with complete coverage for the
  requested range. Backfilled records retain their later observation and
  readiness timestamps and are not misrepresented as live-system history.

The mode cannot be unset or mixed for external evidence and is part of the run
fingerprint. For model-signal time `T`, evidence is selected only when:

```text
mode-specific availability <= T
AND valid_from <= T, when valid_from exists
AND T <= valid_until, when valid_until exists
AND (global relevance OR ticker match OR any sector match)
AND the evidence is policy eligible
```

Ticker, sector, and global scope are a union. One event may contain multiple
approved tickers, multiple reviewed sectors, and global relevance at the same
time; it remains one source fact. Legacy singular-sector events normalize as
before. Plural scope and global/ticker combinations are fingerprinted and no
second applicable sector is discarded.

Lifecycle resolution occurs before cross-source duplicate handling. At `T`,
only the latest deterministically ordered revision of one source fact whose
lifecycle is visible may be considered. The applicability interval is
half-open: a revision is active from its effective/observed time until its
superseding revision. When a new edit has been observed but is not
evidence-ready, neither the edit nor the prior text is active. A latest
`DELETED` or `RETRACTED` revision suppresses prior evidence; ambiguous heads
produce `LIFECYCLE_ORDER_CONFLICT` and fail closed.

After lifecycle and canonical-conflict checks, identical meaningful content
under one canonical owner may collapse to one policy-active fact with only
lineage observations visible by `T`. Source-specific input fingerprints remain
unchanged. Combined SEC/company preparation derives the additive owner only
from exact document, normalized-text, excerpt, and durably recorded trusted
input scope; an older attempt without that scope cannot be cross-source
collapsed by inference. Generated AI output and AI-generated scope never enter
the owner identity, while contradictory outputs under one owner fail closed.
Different text/input stays separate evidence.
Deterministic `ResearchEvidenceRelationship` values may link SEC, IR, RSS, and
earnings observations, but linkage never merges unequal documents, transfers
an earlier availability/readiness time, or makes future member content visible.
Relationships themselves become visible only when both members are ready in
the selected mode. PLTR IR/LMT RSS observations may link to an earnings-page
observation through exact canonical official-URL or meaningful-content
metadata; equal URL alone never makes unequal text interchangeable.

When `valid_until` is absent, the run's explicit finite maximum age is also
required.  Filing dates, transaction dates, collection times, and later archive
load times never substitute for canonical public availability.  Exclusion
audit distinguishes future, expired, outside-lookback, missing-availability,
scope, profile, malformed, and policy-ineligible evidence.  Complete hydration
exclusions remain in the run-level audit; a decision receives only exclusions
whose recorded scope and time are applicable to that ticker and evaluation
time.

## Policy and fingerprints

The injected policy is an ordered list of exact-match rules over:

```text
AI_EVENT_TYPE:<value>
DETERMINISTIC_EVENT_TYPE:<value>
FLAG_TYPE:<value>
```

All eligible evidence is selected first.  Rules are tested in declared order
against the complete selected set.  The first matching rule supplies the one
hypothetical action, and every canonically ordered item matching that winning
rule is recorded.  Lower-priority rules are ignored.  Selected non-winning
evidence remains in the shadow fingerprint but not in the matched-action IDs.
No matching rule means `NO_CHANGE` and empty matched IDs.

The combined shadow fingerprint covers the existing
`DecisionContext.context_fingerprint`, selected normalized event evidence,
as-of-visible relationships and lineage, pinned source/profile/coverage and
archive/resolution state, availability/lifecycle/correlation settings, the
finite applicability setting, and every other run setting that changes
selection. It does not alter the structured fingerprint. New fields are
conditional: an unchanged legacy SEC-only run retains its prior run payload,
selection behavior, and fingerprint. Policy version and policy configuration
hash remain separate fields.

The deterministic shadow-evaluation identity covers the model signal, optional
risk decision, exact evaluation time, combined fingerprint, policy version,
and policy hash.  QuestDB remains an append-only audit path; its table does not
enforce uniqueness for this logical identity.

## Offline check

Run the complete fixture flow without network, Gemini, QuestDB, broker, or real
risk changes:

```powershell
& ".\.venv\Scripts\python.exe" scripts/check_context_shadow_evaluation.py
```

Writing the resulting structured shadow row to the existing QuestDB table is a
separate explicit opt-in with `--questdb`.

The complete external pilot checker is also offline by default:

```powershell
& ".\.venv\Scripts\python.exe" scripts/check_external_event_sources.py
```

It exercises fixture-backed connectors, archive publication, classification
ownership, hydration, as-of selection, and default `NO_CHANGE` without network,
Gemini, QuestDB, Alpaca, or real risk changes.
