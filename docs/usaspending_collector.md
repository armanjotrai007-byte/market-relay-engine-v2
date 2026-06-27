# USAspending Contract-Award Collector

PR28 adds a disabled-by-default, one-shot USAspending contract-award collector for
future profitability research. It records factual contract-award evidence for
explicitly mapped public-company prime recipients. It does not trade, score,
approve, block, resize, or route orders.

## Source And Credentials

USAspending does not require an API key for the collector endpoints used here.
There is no `USASPENDING_API_KEY`, no credential environment variable, and no
website scraping.

The collector uses only official USAspending endpoints:

- `GET /api/v2/awards/last_updated/`
- `POST /api/v2/search/spending_by_award/`
- `GET /api/v2/awards/{award_id}/`
- `POST /api/v2/awards/funding/`

The last-updated endpoint returns a source-health date. The collector accepts
the supported source-health representations `YYYY-MM-DD` and `MM/DD/YYYY`. This
date is not an award publication timestamp, market availability timestamp,
award-level revision feed, or proof that all awards are complete.

## Recipient Mapping

Mappings live in `config/usaspending_recipient_ticker_map.yaml`.

```yaml
mapping_version: usaspending_recipient_map_v1
recipients:
  - recipient_uei: "EXACT_OFFICIAL_UEI"
    recipient_name: "EXACT OFFICIAL LEGAL RECIPIENT NAME"
    ticker: "PUBLIC_TICKER"
    issuer_name: "PUBLIC ISSUER NAME"
    mapping_confidence: confirmed
    economic_beneficiary: prime_recipient
    active: true
    mapping_version: usaspending_recipient_map_v1
```

Recipient UEI is the identity key. Recipient name is verification metadata only.
The USAspending search input uses `recipient_search_text`, whose official
semantics are text search across recipient name, UEI, and DUNS. PR28 therefore
uses:

```text
recipient_discovery_method = text_search_then_exact_uei_verification
recipient_search_is_complete_coverage = false
```

Every search row whose returned `Recipient UEI` does not exactly match the
configured UEI is ignored as selection noise. Every enriched detail response
whose `recipient.recipient_uei` does not exactly match the configured UEI is a
data-integrity issue and no event is emitted.

No fuzzy matching, DUNS fallback, parent-recipient inference, LLM matching,
website lookup, headline matching, or manual string similarity is used.

## Discovery Window

PR28 discovers current records by USAspending `last_modified_date`, not
`action_date`.

```text
ny_collection_date = collector_observed_at converted to America/New_York
end_date = ny_collection_date
start_date = ny_collection_date - (lookback_days - 1)
filters.time_period = [{start_date, end_date, date_type = last_modified_date}]
```

`last_modified_date` is used to find newly surfaced or revised government
records in a bounded current window. `action_date` and `date_signed` are source
business-date evidence only. They are not discovery timing and not market
availability timing.

The collector requests only page 1 of search results. If USAspending reports
more pages, or if exact-UEI candidates exceed the configured enrichment cap, the
run is partial and `coverage_complete=false`.

## Award Values And Funding Evidence

The collector preserves the official award-detail amounts separately when
returned:

- `total_obligation_usd`
- `base_exercised_options_usd`
- `base_and_all_options_usd`

These labels are deliberately narrow. `total_obligation_usd` is not incremental
obligation. `base_and_all_options_usd` is not funded obligation.

Award funding is a bounded first page of official funding records:

```text
page = 1
limit = funding_limit_per_award
sort = reporting_fiscal_date
order = desc
```

Funding records are evidence, not a current transaction signal and not a
full-award funding total. If funding has another page, the event may still be
recorded with factual first-page evidence, but the run is partial and the event
states `funding_page_complete=false`.

No missing numeric amount is coerced to zero.

## Availability And Event Studies

The collector stores two different concepts:

- `source_event_time`: UTC midnight of the source business date when available.
- `collector_observed_at`: the actual UTC collection time when the API response
  was observed.

Future profitability studies must anchor returns only after
`collector_observed_at`. They must not backtest as though a position could have
been entered at `action_date`, `date_signed`, or the source last-updated date.

Every event details payload includes:

```text
availability_basis = collector_observed
historical_action_date_asof_eligible = false
forward_outcome_anchor_time = collector_observed_at
forward_outcome_study_eligible = true
source_last_updated_is_precise_publication_time = false
```

## Cache, Ledger, And Checkpoint

Each award uses an award-specific ticker cache name:

```text
usaspending:contract_award:<TICKER>:<CANONICAL_AWARD_ID>
```

`CANONICAL_AWARD_ID` is the exact non-empty
`detail.generated_unique_award_id`. Multiple awards for one ticker coexist in
`ContextStateCache`. A same-award revision may replace only that award's cache
entry.

All QuestDB rows use the fixed indicator name:

```text
usaspending_contract_award_event_v1
```

Award-level uniqueness lives in `context_indicator_id` and details. This keeps
QuestDB event-study grouping stable.

USAspending cache entries use `severity=INFO` and `valid_until=null`, so a
research horizon passing does not create expired-context risk elevation. The
cache is a current-process convenience. QuestDB remains the durable historical
research ledger.

The JSON checkpoint is bounded dedupe and revision state for events whose
required QuestDB ledger write has already succeeded. Non-durable/offline runs,
including runs with `write_questdb=false`, may return snapshots and update the
current-process cache, but they do not permanently mark new award events as seen
and therefore do not suppress a later durable QuestDB write. If a QuestDB write
is requested but the writer is unavailable or fails, the new event is not added
to checkpoint event/registry dedupe state.

QuestDB remains the canonical durable research ledger. When a requested QuestDB
context-indicator write fails after a USAspending award event has been built,
the collector appends a self-contained emergency preservation row to the
configured JSONL fallback directory:

```text
data/emergency_ledger/YYYYMMDD.jsonl
```

The JSONL row preserves the intended `ContextIndicatorSnapshot`, safe
run/session metadata, the deterministic `context_indicator_id`, and a stable
primary-write failure code. The `YYYYMMDD` filename is based on the UTC time the
fallback row is written. This fallback is not a successful QuestDB write and
does not grant primary-ledger checkpoint completion. It is intentionally a tiny
append-only emergency mechanism, not an automatic replay queue, importer,
retry worker, or distributed transaction system. Future reconciliation can use
the deterministic record IDs to avoid duplicate canonical QuestDB ledger rows.

For durable runs, the collector writes the QuestDB snapshot first and then
records the event fingerprint in the JSON checkpoint. This is a practical
ordering boundary, not an exactly-once distributed transaction: if the process
or filesystem fails after a successful QuestDB write but before checkpoint
persistence, a later run may retry the same event.

When a revision recheck successfully processes durable-safe checkpoint changes,
those changes are written even if every normal recipient-search path failed.
That search-outage persistence does not advance
`last_successful_collection_at`, source-wide discovery metadata, or other
signals that would claim the full discovery collection succeeded. QuestDB remains
the durable historical research ledger; the JSON checkpoint remains bounded
dedupe/revision state.

Checkpoint exclusivity is enforced by an OS-backed non-blocking advisory lock on
the stable lock file:

```text
data/usaspending/award_checkpoint.json.lock
```

The physical `.lock` file may remain after a crash or restart. That stale file
is harmless because liveness is determined by the operating-system lock, which
is released when the owning process dies. Concurrent active collectors still
fail closed: lock contention raises a busy error and performs no HTTP, cache
write, ledger write, or checkpoint write. Lock-file owner metadata is diagnostic
only and is not used to decide whether a lock is live.

Checkpoint retention is bounded:

- event fingerprints: 45 calendar days after event first observation;
- award registry: `award_registry_retention_calendar_days`;
- revision rechecks: `revision_recheck_calendar_days`.

After registry retention expires, PR28 makes no duplicate or revision detection
claim unless the award re-enters bounded discovery.

## Revisions

The deterministic `context_indicator_id` is derived from the canonical semantic
identity payload, including award ID, ticker, UEI, stable classification, award
type, source business date, official amount fields, award last-modified date,
funding completeness, returned funding record count, and the funding evidence
fingerprint.

Identical replay keeps the same ID, writes no QuestDB row, and can rehydrate
the cache. A same-award semantic change gets `AWARD_REVISION_DISCOVERED`, a new
ID, and one eligible new QuestDB row.

`NEW_AWARD_DISCOVERED` and `LATE_OR_BACKFILL_DISCOVERY` are first-discovery
timeliness classes. They are persisted and do not drift on later replay.

## Zero-Event Success

A successful run with zero events means only:

```text
No exact mapped recipient award was newly emitted inside the bounded configured
coverage.
```

It does not mean no government contracts exist, no relevant awards exist outside
configured mappings, no USAspending records were updated outside the window, or
no market-moving government news exists.
