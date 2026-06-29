# Context Provenance Contract

Structured context details may include a canonical provenance object at:

```python
details["provenance"]
```

The V1 contract version is `context_provenance_v1`. All fields are always present
and all timestamps are UTC ISO strings ending in `Z` or `null`.

## Fields

- `provenance_version`: fixed contract version.
- `source_event_time`: the upstream source event or observation time. This is not
  public availability.
- `source_observed_at`: a real source-provided observation timestamp when one
  exists; it is not synthesized from polling time.
- `available_at`: proven public availability time, or `null` when unknown.
- `collected_at`: local collection audit time for the accepted record.
- `effective_from`: deterministic decision-effect boundary when one exists.
- `valid_until`: JSON audit mirror of `ContextStateEntry.valid_until`.
- `availability_basis`: why `available_at` is known or why it remains collector
  observed only.
- `research_asof_eligible`: whether this record may be used in historical as-of
  research.
- `revision_id` and `vintage_id`: optional source-provided revision/vintage IDs;
  they are not invented.
- `source_record_id`: stable source record identity when one exists.

## Eligibility Rules

Research as-of eligibility is strict:

```text
research_asof_eligible is true
AND available_at exists
AND available_at <= decision_time
```

Observation dates, award dates, source business dates, source last-updated dates,
and collection times are not proof of public availability.

Active-window eligibility is inclusive:

```text
effective_from <= decision_time <= valid_until
```

If either boundary is absent, the item is inactive. This mirrors the existing
cache expiry policy where an entry is visible through `valid_until`; cache
`valid_until` remains the runtime authority.

## Semantic Identity

`collected_at`, top-level `freshness_seconds`, and top-level
`collector_observed_at` are audit-only for duplicate comparison. Changing only
those fields must not replace a cache entry, change deterministic source IDs, or
create duplicate ledger rows. Semantic source facts such as `available_at`,
`effective_from`, `valid_until`, `availability_basis`, `source_record_id`,
`revision_id`, `vintage_id`, `event_first_observed_at`, and
`forward_outcome_anchor_time` remain part of semantic comparison.

## Source Policies

- EIA reviewed configured releases may use `available_at=release_at`,
  `availability_basis=official_release_timestamp`, and
  `research_asof_eligible=true`. Report periods remain source evidence only.
- FRED uses observation-date source-event evidence, `available_at=null`, and
  `research_asof_eligible=false`. FRED remains historical-as-of ineligible until
  vintage-aware work exists.
- USAspending uses award/action/business dates only as source-event evidence,
  `available_at=null`, and `research_asof_eligible=false`. Forward outcome
  anchoring uses stable first observation, not current polling time.
- yfinance remains development-only and non-authoritative. Completed bar-end is
  source-event evidence; completion grace may define `effective_from`, but
  `available_at=null` and `research_asof_eligible=false`.

Raw context remains research evidence only. It does not grant trade approval,
risk blocking, sizing, model, execution, or other direct authority.
