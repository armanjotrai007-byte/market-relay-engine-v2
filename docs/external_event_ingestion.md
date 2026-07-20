# External Event Ingestion Pilot

This pilot brings four official news/social inputs into the existing PR35–PR37
research-evidence path:

1. Donald Trump Truth Social posts delivered by VeritaWire.
2. Lockheed Martin official all-news RSS and linked releases.
3. Palantir official investor-relations releases.
4. Official PLTR and LMT earnings releases.

It does not add the other eight stock adapters, a general news platform, or any
trading authority.

## Research-only boundary

External records may be archived, normalized, classified, validated, projected
to `ResearchEvidence`, selected in memory, and evaluated by the existing shadow
policy. They must never:

- Change model output or the deterministic risk filter.
- Approve, block, resize, or delay a real order.
- Call Alpaca or another broker.
- Change positions.
- Perform network or disk I/O during per-signal selection.

The default shadow policy remains `NO_CHANGE`. Multiple matching records still
produce one current shadow action; there is no count voting, severity summation,
duplicate-weighted confidence, or assumption that more articles imply a
stronger effect.

## Official sources

### VeritaWire / Truth Social

- Product and pricing: `https://veritawire.com/`
- Documentation: `https://veritawire.com/docs`
- Terms: `https://veritawire.com/terms`
- WebSocket: `wss://veritawire.com/ws`
- Authentication: `Authorization: Bearer <VERITAWIRE_API_KEY>`

The free authenticated feed is delayed by approximately ten seconds and
supports bounded recent-buffer replay with `last_seen_id`. The key must be sent
only in the header. `VERITAWARE_API_KEY` is a spelling error; committed code and
configuration use only `VERITAWIRE_API_KEY`.

The connector relies only on fields documented by the official site: post ID,
account username, creation time, and content. A bounded authenticated smoke
test confirmed connection acceptance, but no post arrived during that window,
so the complete live message and lifecycle-notice schemas remain unverified.
Unknown or type-drifted messages fail closed after their raw bytes have been
durably archived when the minimum source envelope can be proved. Raw messages
are never printed merely for inspection.

### Lockheed Martin

- RSS directory: `https://news.lockheedmartin.com/rss`
- All-news feed:
  `https://news.lockheedmartin.com/news-releases?pagetemplate=rss`
- Quarterly results:
  `https://investors.lockheedmartin.com/financial-information/quarterly-results/`

The all-news feed is primary because contracts, programs, operations, and other
material releases are not necessarily categorized as investor news. The feed
item description is not treated as the full release when an official linked
article is available. Current feed items may omit GUIDs, so the normalized
canonical official article URL is the fallback source-fact identity.

### Palantir

- Release page:
  `https://investors.palantir.com/news-events/news-releases/`
- Events and earnings: `https://investors.palantir.com/events.html`
- Official year-list endpoint:
  `https://investors.palantir.com/feed/PressRelease.svc/GetPressReleaseYearList?languageId=1`
- Official release-list endpoint pattern:
  `https://investors.palantir.com/feed/PressRelease.svc/GetPressReleaseList?languageId=1&bodyType=1&year=<YEAR>&includeTags=true&pressReleaseDateFilter=1`

The release page is client-rendered. Its own application uses the same-origin
JSON endpoints above, which provide a stable `PressReleaseId`, revision number,
publication time, canonical detail path, and official HTML body. This is the
simplest official read-only mechanism; browser automation is not required.

For earnings, the PLTR events page and LMT quarterly-results page identify the
official Earnings Release. Webcasts, podcasts, audio, transcripts, analyst
estimates, and surprise calculations are outside this pilot. Supporting links
may be recorded as metadata but are not independently classified by default.

An earnings occurrence is keyed by ticker, fiscal year, and fiscal quarter.
Each discovered official document remains a separate immutable source record.
Package metadata is also occurrence-versioned with a package content hash,
monotonic revision sequence, supersession link, and immutable package revision
ID. Reappearance of earlier metadata after an intervening change is a new
occurrence revision rather than an overwrite.

The implementation reuses `requests`, uses `websockets>=16.1,<17` for the
authenticated socket, `beautifulsoup4>=4.15,<5` with the standard HTML parser
for deterministic text extraction, and `pypdf>=6.14,<7` for text-based PDFs.
RSS/Atom XML uses bounded standard-library parsing with DTD/entity rejection.
Playwright, OCR, and overlapping HTTP/parser stacks are not dependencies.

## Data flow and durable archive

```text
source connector
-> immutable raw object and source observation
-> immutable source fact/revision metadata
-> deterministic normalization and bounded excerpt
-> existing ContextRawInput / ContextSourceDocument
-> existing ContextClassificationRequest
-> existing PR35 Gemini classifier and validator
-> durable canonical classification publication
-> validated ContextAIEvent
-> explicit PR37 preparation and one bounded ResearchEvidenceIndex
-> memory-only as-of selection
-> existing ShadowContextPolicyEvaluation
-> existing metadata-only QuestDB writer/fallback
```

The ignored archive root is:

```text
data_lake/context/external_events/
```

It contains content-addressed raw and normalized objects, immutable source
observations and revisions, earnings package metadata, canonical
classification attempts/claims/conflicts/resolutions, coverage records, and
atomic mutable manifests/checkpoints. Exact layout is owned by the archive
implementation; consumers must not infer state from filenames.

Archive publication precedes replay/checkpoint advancement. If archive
publication fails, the checkpoint cannot advance. If checkpoint publication
fails after archival, replay is harmless. The durable manifest—not an
in-process queue—is the restart-safe pending-work mechanism.

QuestDB and emergency JSONL contain metadata only. Raw payloads, articles,
PDFs, normalized text, excerpts, prompts, provider bodies, full exceptions, and
credentials remain in the ignored archive or process memory as appropriate.

## Timestamp model

Every source revision preserves distinct meanings:

- `source_available_at`: earliest trusted source/public availability of that
  revision.
- `system_observed_at`: when this collector received or finished fetching the
  revision.
- `archived_at`: successful immutable source publication.
- `normalized_at`: deterministic normalization completion.
- `classified_at`: provider classification completion.
- `validated_at`: strict validation completion.
- `evidence_ready_at`: completion of validation and durable classification/event
  publication.
- `available_at`: policy-facing compatibility time on a returned, ledgered, or
  hydrated external `ContextAIEvent`; it mirrors that revision's durable
  `evidence_ready_at`, never the earlier source-publication time.

`evidence_ready_at` is at least the maximum of observation, normalization,
classification, validation, and durable-save completion. Provider retries,
budget waits, and pending queues delay it. An archived pending record is not
active AI evidence.

The immutable event payload is written before its authoritative readiness
receipt and therefore leaves `available_at` and `evidence_ready_at` null. After
the receipt is durably published, the returned/QuestDB event and PR37 hydration
overlay that exact per-revision time. Reusing an older canonical classification
can never transfer the older observation's readiness to a later revision.

For live VeritaWire records, source creation time is preserved but never used to
hide the free-feed delay. Socket receipt is `system_observed_at`. A changed
record without a trustworthy source update time uses first observation of that
revision rather than backdating the revision to the original publication.

Research runs choose one explicit availability mode:

- `LIVE_SYSTEM_READY`: selection uses `evidence_ready_at`. This is the default
  for the pilot’s live collection.
- `HISTORICAL_SOURCE_TIME`: an explicit counterfactual using
  `source_available_at`; it is allowed only with complete coverage for the
  requested source-time range.

Modes cannot be mixed silently. The selected mode enters the research
fingerprint. Backfilled historical documents retain their historical source
time and current observation/classification/readiness times; they cannot be
described as a historical live-system simulation.

## Lifecycle revisions

Every revisable record contains:

- `source_fact_id`.
- `source_revision_id`.
- `revision_sequence`.
- `supersedes_revision_id`.
- `lifecycle_state`.
- `lifecycle_effective_at`.
- `system_observed_at`.
- `evidence_ready_at`.

For VeritaWire, `source_fact_id` is the underlying Truth Social post ID. The
archive retains exact duplicate deliveries and replay observations separately
from content revisions. Its generic lifecycle model can preserve reviewed
deletion/retraction tombstones without erasing earlier bytes; the connector does
not guess a live deletion-notice schema that VeritaWire has not exposed during
the bounded inspection.

At decision time T, lifecycle resolution happens before cross-source duplicate
handling:

1. Find revisions of one fact effective/observed by T.
2. Resolve the latest unambiguous revision using authoritative revision order
   or the immutable supersession chain.
3. Exclude older revisions as `SUPERSEDED_BY_LIFECYCLE_REVISION`.
4. Emit no evidence when the latest state is `DELETED` or `RETRACTED`.
5. Emit no evidence when the latest revision is not evidence-ready; never fall
   back to older ready text.
6. Fail closed with `LIFECYCLE_ORDER_CONFLICT` when ordering is ambiguous.

Lifecycle applicability is half-open:

```text
effective_from <= T < superseded_at
```

Thus an original may be selected before an edit is observed, neither version is
selected while the observed edit awaits classification, and only the edited
revision is selected after its own readiness time.

## Canonical classification ownership

Every semantic request receives a deterministic
`classification_input_fingerprint`: SHA-256 of canonical request content and
the exact pinned profile, including:

- Document, normalized-text, and excerpt hashes.
- Trusted input ticker, sector, and global scope.
- Adapter, extractor, normalizer, excerpt, and scope versions that affect the
  provider-visible request.
- Prompt, model, response-schema, validator, and classifier-configuration
  versions/hashes.

IDs, timestamps, latency, retries, and generated output are excluded.

The first successfully validated and durably published result atomically claims
that input fingerprint. Later processes and bounded backfills reuse the
canonical result without another Gemini call. Provider failures and validation
failures do not claim ownership and remain retryable. A reused result does not
transfer availability: each source revision receives its own readiness time
after its reuse/event publication completes.

Attempt, canonical-claim, materialized-event, and readiness files are fsynced
before their manifest entries. On restart, identity-validated reconciliation
adopts a file left in that narrow crash window before classification ownership
is evaluated, so durable provider work is not repeated or lost. Resolution
files receive the same treatment when a crash happens before the separate
resolution-manifest save. Research preparation reconciles both manifests before
checking their generation pins; adoption therefore forces a new pin instead of
silently entering an older snapshot.

Each result also has:

- `complete_output_fingerprint`: all normalized generated response fields.
- `policy_output_fingerprint`: fields capable of affecting policy
  materialization.

Different outputs under the same input fingerprint and exact profile produce an
immutable `CLASSIFICATION_CONFLICT` and block preparation. The system never
chooses automatically by latest, majority, confidence, severity, merge, or a
new call under the same profile.

Conflict records contain safe IDs, hashes, conflicting field names, profile
hash, archive chronology, and detection time—never source text or provider
bodies. Resolution is an immutable reviewed artifact:

- `KEEP_FIRST_DURABLY_PUBLISHED`: allowed only when archive chronology proves
  the original canonical live claim predates the contradictory imported or
  backfill attempt. The resolution pins both the complete-output and
  policy-output fingerprints and retains the original `evidence_ready_at`.
- `ABSTAIN_INPUT`: admits neither output and is required when ownership or
  chronology is ambiguous.
- `RECLASSIFY_UNDER_NEW_PROFILE`: preserves old attempts and requires a newly
  pinned profile and result. It never overwrites history.

`ResearchRunDefinition` pins the conflict-resolution manifest generation.
Resolution IDs, chosen complete/policy output hashes, profile hashes, and
generation enter the research fingerprint.

## Semantic classification and union scope

Semantic event classification, scope determination, and policy eligibility are
separate:

- The existing Gemini classifier interprets the event taxonomy.
- Strict versioned scope output is validated against the configured ticker
  universe and reviewed sector allowlist.
- Deterministic alias extraction preserves directly observed approved tickers.
- Policy eligibility additionally requires valid classification, truthful
  scope, durable readiness, admitted profile/source, lifecycle currency, and
  accepted coverage.

The external profile pins prompt `context_filter_v2_scope`, response schema
`context_classification_response_v2`, and validator
`context_filter_validator_v2_scope`. V1 remains unchanged for legacy runs.
Trusted and validated model scope are combined by one deterministic union; the
pilot does not add another Gemini client or retry path.

Every text-bearing Trump post is eligible for Gemini. Keywords do not act as a
semantic gate. Budget exhaustion leaves work pending. Empty/media-only posts
remain archived without OCR, image, or audio interpretation. Every official
LMT, PLTR, and earnings release is eligible because its source ownership fixes
the company ticker.

One event may simultaneously contain:

- Zero or more affected tickers.
- Zero or more affected sectors.
- `global_relevance` true or false.

All explicit approved ticker matches are collected, uppercased, sorted, and
deduplicated. Deterministic matches cannot be removed by AI output. Unknown
tickers, invented sectors, wrong types, and non-boolean global output fail
validation.

Selection is OR-based:

```text
global_relevance
OR decision ticker in affected tickers
OR decision sector in affected sectors
```

One globally, sector-, and ticker-relevant source fact remains one event; it is
not copied into separate ticker-owned facts. New external records use
`ContextAIEvent`. `ContextFlag` is not used to force multi-scope events into its
legacy single-ticker shape.

Legacy singular sector fields continue to normalize as before. New plural
scope and explicit global values are fingerprinted; unchanged SEC-only run
fingerprints remain stable.

## Scope-aware bounded excerpts

Complete normalized text stays in the archive. The classifier receives a
deterministic excerpt bounded by its configured character limit.

For long documents the excerpt includes:

- Title and opening context.
- Bounded context windows around every deterministic span supporting a claimed
  ticker, sector, or global scope.
- Deterministically prioritized material sections.
- Remaining opening/closing material within the fixed budget.

Earnings section priority is:

1. Results highlights.
2. Guidance/outlook.
3. Segment results.
4. Backlog/customer metrics.
5. Margin and cash-flow discussion.
6. Material charges.
7. Operational constraints.

The archive records selected spans/offsets, full and excerpt hashes/counts,
truncation, extraction version, and excerpt version. A deterministic scope
claim is not retained when its supporting source span is absent from the
classifier input. Fixed ticker ownership for official LMT/PLTR sources is the
exception because it is trusted metadata rather than inferred text.

## Exact duplicates and related events

Lifecycle is resolved and canonical classification conflicts are checked before
duplicate grouping.

Canonical classification ownership is strict: one exact source-specific
semantic request and pinned profile owns one `classification_input_fingerprint`.
The attempt also persists its trusted input scope. During combined preparation,
an additive `canonical_classification_owner_fingerprint` may bridge SEC and
company observations only when their document, normalized-text, excerpt, and
trusted-scope inputs are exact. The source-native fingerprints remain intact
for profile audit. Older attempts without durable trusted input scope retain
their source-specific owner and cannot be cross-source collapsed by inference.

Exact observations under that shared deterministic owner may add as-of-visible
lineage to one policy-active fact. Contradictory complete or policy outputs
under the shared owner fail closed; output is never part of duplicate identity
and never decides which owner wins. Any difference in the three meaningful
input hashes or trusted scope keeps the evidence separate.

Different document, normalized-text, or excerpt hashes are related but distinct
evidence. They retain separate IDs, classifications, source/readiness times,
and may both be selected after their own readiness. Deterministic linkage may
record:

- `correlation_group_id`.
- Related event IDs.
- Relationship type.
- Correlation version and effective time.

A ticker/quarter/time-window match is only a relationship candidate. It never
makes unequal SEC and company text interchangeable and never transfers the
earlier record’s availability, scope, summary, or classification to later
content. Relationships are visible only after both members are available under
the run’s selected mode. Separately, PLTR IR or LMT RSS and the corresponding
earnings-page observation may be linked by an exact canonical official URL or
exact meaningful-content metadata. Equal URL with unequal text is linkage only,
not duplicate collapse.

Example:

- SEC results text ready at 16:01.
- Richer company release ready at 16:05.

Before 16:05, only the SEC-derived evidence can appear. Company-only text and
lineage remain invisible. At or after 16:05, both unequal evidence records may
appear and share a non-merging relationship.

## Coverage and backfill

Each source has generation-pinned coverage metadata:

- Coverage start/end and status.
- Status of `LIVE_ONLY`, `PARTIAL`, `COMPLETE_FOR_RANGE`, or `UNKNOWN`.
- Known gaps.
- Bootstrap time and live collection start.
- Completed bounded backfill ranges.
- Last verification time and coverage generation/version.

Research preparation verifies coverage before hydration. Missing ranges or
gaps fail closed by default. An explicit `allow_incomplete_coverage` setting may
permit incomplete live-system research and enters result metadata and the
fingerprint. It cannot make an incomplete source eligible for
`HISTORICAL_SOURCE_TIME`.

The first normal HTTP run may establish a checkpoint and archive bounded
discovery metadata without classifying historical history. It marks coverage
`LIVE_ONLY` or `PARTIAL`, never complete. Historical backfill requires explicit
source, bounds, maximum items, and separate classification opt-in. No default
run classifies an unbounded backlog. Bootstrap checkpoints retain a versioned
per-item or per-package discovery/content hash. An unchanged baseline stays
excluded, while the same stable identity with a changed discovery/content hash
is acquired as a revisable source fact instead of remaining excluded forever.

`LIVE_ONLY` coverage can prove only the continuous interval beginning at the
recorded live-collection start. Completed historical backfill intervals become
eligible only after the manifest status is advanced to `PARTIAL` or
`COMPLETE_FOR_RANGE`; merely attaching an interval to a stale `LIVE_ONLY`
manifest cannot make an earlier historical run complete. Coverage replacement
is atomic. If a process stops after replacing a schema-valid coverage file but
before registering its digest, restart reconciliation adopts that exact file,
advances the archive generation, and invalidates every older research pin.
Malformed, noncanonical, misowned, or concurrently replaced files fail closed.

## Configuration

External sources live under `unstructured_sources` in
`config/context_sources.yaml`:

- `veritawire_truth_social`.
- `lockheed_martin_rss`.
- `palantir_ir`.
- `company_earnings`.

They are disabled by default and declare `direct_trade_authority: false`.
Operational settings include official URL, timeout/size/item bounds, polling or
reconnect limits, archive root, parser/extraction versions, fixed ticker where
applicable, and the API-key environment-variable name.

Initial configured polling targets are 30 seconds for LMT RSS and PLTR IR and
10 seconds for explicitly enabled earnings-window fast polling. One-shot mode
does not sleep, and tests use fake transports/injected clocks.

## Checker and runtime commands

The complete offline fixture-backed pilot check makes no network, Gemini,
QuestDB, Alpaca, or risk call:

```powershell
& ".\.venv\Scripts\python.exe" scripts/check_external_event_sources.py
```

Bounded live source checks are explicit:

```powershell
& ".\.venv\Scripts\python.exe" scripts/check_external_event_sources.py --live --source veritawire --max-items 1 --timeout-seconds 20
& ".\.venv\Scripts\python.exe" scripts/check_external_event_sources.py --live --source lmt-rss --max-items 1 --timeout-seconds 20
& ".\.venv\Scripts\python.exe" scripts/check_external_event_sources.py --live --source pltr-ir --max-items 1 --timeout-seconds 20
& ".\.venv\Scripts\python.exe" scripts/check_external_event_sources.py --live --source earnings --ticker PLTR --max-items 1 --timeout-seconds 20
& ".\.venv\Scripts\python.exe" scripts/check_external_event_sources.py --live --source earnings --ticker LMT --max-items 1 --timeout-seconds 20
```

VeritaWire smoke exits at its finite timeout even when no new post arrives. It
may validate authenticated connection/replay without waiting for Trump to post.

`--classify` and `--questdb` are independent explicit opt-ins. Source
connectivity alone performs neither action. `--backfill` requires bounded
selection such as `--max-items`; classification still requires `--classify`.
Optional polling requires `--poll` and a bounded `--max-polls` for checks.

Run `--help` against the checked-out implementation before live use. A live
check never calls Alpaca or changes risk. It reports what source, archive,
classification, and ledger actions were and were not exercised without
printing secrets or raw bodies.

### Bounded live verification snapshot

The delivery checks on 2026-07-18 (America/Toronto) used `--max-items 1`, a
finite timeout, and no `--classify` or `--questdb` flag:

- VeritaWire accepted the authenticated WebSocket connection and exited at its
  20-second bound with zero messages. Authentication/connectivity is verified;
  a complete live post or lifecycle-notice payload was not observed.
- LMT all-news RSS discovered and archived one bounded official article. The
  immediate second run received a conditional not-modified response.
- PLTR IR and PLTR earnings discovery had already acquired bounded official
  records in this implementation session; repeated runs received conditional
  not-modified responses.
- The LMT quarterly-results HEAD request returned HTTP 200, and ordinary source
  inspection showed the expected current quarter/press-release structure.
  However, bounded local Python and `curl` GET requests timed out before any
  response bytes (60 and 30 seconds respectively), so the LMT earnings archive
  smoke could not be completed from this workstation. The adapter failed
  closed and admitted no content.

No live smoke called Gemini, QuestDB, Alpaca, the risk filter, or an execution
path. The local ignored file still uses the reported `VERITAWARE_API_KEY` typo;
rename it to `VERITAWIRE_API_KEY` before using the normal VeritaWire command.

## Restart behavior and health

Safe status includes source, enablement, last successful connection/poll,
source identity, receipt/publication times, failure category/count, reconnects,
duplicates, new/pending/completed counts, parser version, and checkpoint
generation. HTTP adapters persist this body-free state in a dedicated mutable
health checkpoint, so a restarted operator process can distinguish a healthy
unchanged poll from repeated parser, extraction, timeout, or transport failure.

Recovery rules:

- Archive failure: leave checkpoint unchanged.
- Checkpoint failure after archive: replay and suppress safely.
- Provider/budget failure: remain pending.
- Valid/abstained canonical result: suppress repeat provider work across
  restarts.
- QuestDB failure: retain durable result and retry ledger publication without
  reclassification.
- Parser/schema drift or lifecycle/classification conflict: fail closed and
  preserve audit metadata.

When an HTTP response is archived but its parser, schema check, or extractor
fails, the adapter publishes a safe rejected-observation record containing the
raw object hash, stage, failure category, status/content type, approved URL, and
observation time. The rejection record never embeds the response body or an
exception string; the immutable object remains the retryable audit source.

No status, exception, source URI, archive object, or ledger row may contain the
VeritaWire or Gemini key.

## Known limitations

- VeritaWire’s protected documentation may require an authenticated account
  session. Runtime behavior not confirmed by official docs or a bounded live
  inspection is rejected rather than guessed.
- The pilot does not fetch arbitrary Truth Social links or media and performs no
  OCR/audio interpretation.
- PLTR’s release endpoint is official and used by its client application, but
  schema drift remains possible and is detected fail-closed with fixtures.
- The LMT quarterly-results page was structurally verified, but its full GET
  timed out from the delivery workstation as recorded above. Re-run the bounded
  LMT earnings smoke from the deployment network before calling that connector
  live-verified.
- PDF support is limited to deterministic extraction from text-based official
  PDFs.
- Coverage begins as live-only/partial until an explicit bounded backfill is
  completed and verified.
- Related evidence is not a claim of economic equivalence or profitability.
- Only PLTR and LMT company adapters are included. Later stocks must reuse the
  shared archive, polling, profile, coverage, and PR37 preparation seams rather
  than create separate pipelines.
