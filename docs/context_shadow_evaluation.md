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
- Future news or social evidence may use this path only after raw input passes
  through the existing validation/classification pipeline and becomes a
  `ContextAIEvent` or an explicitly event-owned `ContextFlag`.

The research-run definition declares admitted event sources and evidence
categories.  Each normalized event fact is owned by its canonical
`(source, source_record_id)`; a collision on that identity stops hydration.
Any source appearing in both event evidence and the supplied
`DecisionContext` also aborts evaluation.  Structured entries are never copied
or silently deduplicated into the event index.

## Preparation and in-memory evaluation

Archive I/O is a preparation step, not a signal-time operation:

```text
explicit research-run definition
-> read durable PR36 SEC records
-> validate ownership and classification profile
-> normalize safe evidence metadata
-> atomically hydrate a bounded in-memory index
-> freeze the index
-> evaluate model signals from memory only
```

The run definition explicitly supplies its ticker universe, admitted sources
and categories, hydration time range, classification profile, finite maximum
age for evidence without native expiry, and capacity.  Hydration never scans
an unbounded SEC history by default.  Its start must be at least one explicit
lookback before the earliest signal the run will evaluate; incomplete coverage
is rejected.  After hydration, selection and evaluation perform no filesystem,
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

## Leak-free selection

For model-signal time `T`, evidence is selected only when:

```text
available_at <= T
AND valid_from <= T, when valid_from exists
AND T <= valid_until, when valid_until exists
AND the ticker, sector, or global scope applies
AND the evidence is policy eligible
```

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
`DecisionContext.context_fingerprint`, selected normalized event evidence, the
pinned classification profile, the finite applicability setting, and every
other run setting that changes selection.  It does not alter the structured
fingerprint.  Policy version and policy configuration hash remain separate
fields.

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
