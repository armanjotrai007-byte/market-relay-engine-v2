# Market Relay Engine V2 Handoff

## Current work

Repository: `armanjotrai007-byte/market-relay-engine-v2`

Feature branch:

```text
agent/external-event-ingestion-pilot
```

The branch starts from the merged PR37 main commit:

```text
abb39fe64ae13094ec0d038bfbc01eae89c86713
```

Current work adds the first official external news/social pilot through the
existing PR35 classifier, PR36 archive pattern, and PR37 research-only shadow
evaluation seam. Sources are limited to VeritaWire-delivered Trump posts, LMT
official all-news RSS, PLTR official investor releases, and official PLTR/LMT
earnings releases.

Read `docs/external_event_ingestion.md` before changing this pilot and
`docs/development.md` before running or reporting validation.

## Non-negotiable boundaries

- Historical market truth is official Databento DBN/Parquet-derived data, not
  QuestDB.
- Historical and live paths share the canonical feature builder.
- Historical replay still uses batch sorting vs live arrival order: historical
  feature replay sorts by event time, while live processing preserves arrival
  order. Both paths continue to use the same canonical feature builder.
- Signal-time decisions read bounded memory only. They never query QuestDB,
  source archives, or external networks.
- QuestDB stores bot/audit metadata, not raw market data, source documents,
  normalized text, excerpts, prompts, or provider bodies.
- AI context and external sources are research-only. They cannot change model
  output, real risk decisions, order timing/size, Alpaca calls, or positions.
- The deterministic Python risk filter remains final pre-trade authority.
- Alpaca remains paper-first and live trading is disabled by default.
- Default shadow policy remains `NO_CHANGE`.
- No credentials, `.env`, downloaded data, generated archives/manifests, full
  provider exceptions, or raw source bodies belong in Git or QuestDB.

## Current configuration

The tradable universe remains:

```text
PLTR LMT RTX GD AVAV XOM OXY SLB COP VLO
```

The external pilot adds disabled-by-default unstructured sections:

```text
veritawire_truth_social
lockheed_martin_rss
palantir_ir
company_earnings
```

Each declares `direct_trade_authority: false`. Initial normal LMT/PLTR polling
is 30 seconds; explicitly enabled earnings-window fast polling is 10 seconds.
One-shot mode remains available and tests use fake transports/injected clocks.

The only supported VeritaWire environment name is:

```text
VERITAWIRE_API_KEY
```

The user’s ignored `.env` was reported to contain the misspelled
`VERITAWARE_API_KEY`. Rename that local entry when necessary. Never display or
commit its value. `.env.example` contains only a blank safe placeholder.

## External research flow

```text
official source connector
-> immutable raw source archive and observation
-> deterministic source revision/lifecycle state
-> normalized text and scope-aware bounded excerpt
-> ContextRawInput / ContextSourceDocument
-> ContextClassificationRequest
-> existing PR35 Gemini classifier and validator
-> durable canonical classification claim
-> validated ContextAIEvent
-> explicit combined PR37 preparation
-> one bounded frozen ResearchEvidenceIndex
-> memory-only as-of selection
-> ShadowContextPolicyEvaluation (default NO_CHANGE)
```

The receiver/archive path is independent of Gemini latency. Archive publication
precedes replay checkpoint advancement. Valid/abstained canonical results are
saved before optional QuestDB publication and are reused across restarts;
provider or validation failures remain retryable.

## Time and lifecycle correctness

External source revisions preserve at least:

```text
source_available_at
system_observed_at
archived_at
normalized_at
classified_at
validated_at
evidence_ready_at
```

New live runs use `LIVE_SYSTEM_READY` and cannot select an AI event before its
durable `evidence_ready_at`. `HISTORICAL_SOURCE_TIME` is an explicit
counterfactual permitted only with complete source-time coverage. Backfilled
records retain historical source publication and current observation/readiness
times; they are not historical live-system simulations.

Every revisable fact preserves source fact/revision IDs, sequence,
supersession, lifecycle state/effective time, observation, and readiness. At
time T, PR37 resolves the current lifecycle version before cross-source exact
duplicates:

- Older versions are `SUPERSEDED_BY_LIFECYCLE_REVISION`.
- A current deletion/retraction emits no active evidence.
- A current observed edit that is not ready suppresses the older version; there
  is no fallback.
- Ambiguous ordering fails closed with `LIFECYCLE_ORDER_CONFLICT`.
- Applicability is half-open: `effective_from <= T < superseded_at`.

## Classification ownership and conflicts

`classification_input_fingerprint` hashes the exact canonical semantic request
and pinned profile: source document/normalized/excerpt hashes, trusted input
scope, relevant adapter/extractor/normalizer/excerpt/scope versions, prompt,
model, response schema, validator, and classifier configuration. It excludes
IDs, timestamps, latency, and generated output.

The first validated and durably published result atomically owns the input.
Later processes and backfills reuse it without another Gemini call.

Restart reconciliation adopts identity-validated orphan attempts, canonical
claims, materialized events, readiness receipts, and reviewed resolutions left
between immutable file publication and mutable manifest save. Preparation
reconciles before generation checks, so adoption invalidates an older run pin.

Complete and policy output fingerprints detect contradictory imported results.
One input/profile with differing output becomes `CLASSIFICATION_CONFLICT` and
blocks preparation until a reviewed immutable resolution is pinned:

- `KEEP_FIRST_DURABLY_PUBLISHED` only with proven archive chronology and pinned
  complete/policy output fingerprints.
- `ABSTAIN_INPUT` when ownership/chronology is ambiguous.
- `RECLASSIFY_UNDER_NEW_PROFILE` with a newly pinned profile and preserved old
  attempts.

Research runs pin the conflict-resolution manifest generation. Resolutions and
chosen complete/policy output and profile hashes enter the research
fingerprint.

## Scope, duplicate, correlation, and coverage rules

- Semantic event classification, scope determination, and policy eligibility
  are separate.
- Every text-bearing Trump post and every official LMT/PLTR/earnings release is
  classification-eligible. Keyword lists are not a pre-Gemini semantic gate.
- Scope is a union of all approved deterministic ticker matches, validated AI
  tickers/sectors, fixed company ownership, and explicit global relevance.
- One event may be global, multi-ticker, and multi-sector simultaneously.
- Selection matches global OR ticker OR sector. New external news uses
  `ContextAIEvent`, not duplicate `ContextFlag` evidence.
- Long excerpts include title/opening material and the spans supporting every
  claimed deterministic scope; earnings excerpts prioritize results, guidance,
  segments, backlog, margins/cash flow, charges, and constraints.
- Lifecycle and canonical conflict checks happen before exact duplicates.
- Exact document/normalized/excerpt content plus durable trusted input scope
  may share one additive canonical owner across SEC/company observations and
  collapse to one policy-active fact with only as-of-visible lineage. Source-
  specific classifier fingerprints remain intact; missing legacy scope does
  not collapse by inference, and generated output never defines ownership.
- Different text/excerpt hashes stay separate evidence. Fiscal quarter/ticker/
  time proximity may create a relationship only; it never transfers earlier
  availability or later company text. Exact official URLs/content metadata may
  link PLTR IR or LMT RSS to earnings-page observations without merging unequal
  text.
- Coverage is generation-pinned as `LIVE_ONLY`, `PARTIAL`,
  `COMPLETE_FOR_RANGE`, or `UNKNOWN`, with explicit gaps and backfill ranges.
  Incomplete requested coverage fails by default.

## QuestDB deployment

QuestDB remains metadata-only. New nullable metadata columns are appended to
existing layouts; old column prefixes/order remain unchanged. Any schema change
must update together:

- Writer `TABLE_COLUMNS` and row converters.
- Destructive reset schema for disposable databases.
- Ordered additive migration for persistent ledgers.
- Migration validator and `scripts/check_questdb_schema.py`.
- Writer/schema tests and docs.

For a persistent server, stop writers, back up, record row counts/columns, apply
all additive migration files not yet deployed in filename/order, rerun them to
prove idempotence, validate unchanged legacy counts and expected suffixes, then
run schema/writer checks before restarting. Never use
`db/schema/questdb_ledger_v1.sql` as an upgrade; it is destructive.

## Validation

Use the repository interpreter:

```powershell
& ".\.venv\Scripts\python.exe" scripts/check_environment.py
& ".\.venv\Scripts\python.exe" scripts/check_config.py
& ".\.venv\Scripts\python.exe" scripts/check_contracts.py
& ".\.venv\Scripts\python.exe" scripts/check_gemini_context.py
& ".\.venv\Scripts\python.exe" scripts/check_sec_edgar.py
& ".\.venv\Scripts\python.exe" scripts/check_context_shadow_evaluation.py
& ".\.venv\Scripts\python.exe" scripts/check_external_event_sources.py
& ".\.venv\Scripts\python.exe" scripts/check_questdb_schema.py
& ".\.venv\Scripts\python.exe" scripts/check_questdb_writer.py
& ".\.venv\Scripts\python.exe" -m pytest
git diff --check
```

Repository-wide validation is:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_tests.ps1
```

That runner requires local QuestDB under current configuration. If unavailable,
report it as an environmental blocker and list the offline checks that passed.
No lint, formatter, typecheck, packaging-build, or committed CI workflow exists.

Offline external fixtures are the default. Bounded live source checks use:

```powershell
& ".\.venv\Scripts\python.exe" scripts/check_external_event_sources.py --live --source veritawire --max-items 1 --timeout-seconds 20
& ".\.venv\Scripts\python.exe" scripts/check_external_event_sources.py --live --source lmt-rss --max-items 1 --timeout-seconds 20
& ".\.venv\Scripts\python.exe" scripts/check_external_event_sources.py --live --source pltr-ir --max-items 1 --timeout-seconds 20
& ".\.venv\Scripts\python.exe" scripts/check_external_event_sources.py --live --source earnings --ticker PLTR --max-items 1 --timeout-seconds 20
& ".\.venv\Scripts\python.exe" scripts/check_external_event_sources.py --live --source earnings --ticker LMT --max-items 1 --timeout-seconds 20
```

Live checks do not call Gemini or QuestDB unless `--classify` or `--questdb` is
separately supplied, and they never call Alpaca.

## Delivery validation snapshot

Validation completed on 2026-07-18 (America/Toronto):

- Environment, configuration, contracts, Gemini offline, SEC offline, PR37
  shadow, external-source offline, QuestDB schema, and QuestDB writer checkers
  passed.
- The focused external-ingestion suite passed (214 tests).
- The full suite passed: 1,865 tests with 69 existing yfinance/NumPy timedelta
  deprecation warnings.
- `scripts/run_tests.ps1` passed, including the configured local QuestDB health
  check and the full suite.
- VeritaWire accepted authenticated connection within the bounded smoke but no
  post arrived; no complete live message/lifecycle shape was observed.
- LMT RSS acquired one bounded official article and the immediate repeat used a
  conditional not-modified response.
- PLTR IR and PLTR earnings bounded acquisition were verified, with repeated
  checks returning conditional not-modified responses.
- The LMT quarterly-results HEAD request returned HTTP 200 and the official page
  structure was inspected, but local Python and `curl` GET requests timed out
  before receiving response bytes. The LMT earnings archive smoke therefore
  remains an explicit workstation/network blocker; the adapter failed closed.
- No live check enabled Gemini, QuestDB, Alpaca, risk changes, or execution.

The final delivery audit confirmed a clean `git diff --check`, no tracked
credential/archive/downloaded payload, no risk/model/execution path change, and
an unchanged latest `origin/main` base at PR37. The ignored local `.env` still
uses `VERITAWARE_API_KEY`; rename that entry to `VERITAWIRE_API_KEY` without
exposing its value.

Follow-up after this pilot is to add the remaining company adapters by reusing
the shared archive, polling, lifecycle, profile, coverage, and PR37 preparation
seams. It is not to add trading assumptions or profitability claims.
