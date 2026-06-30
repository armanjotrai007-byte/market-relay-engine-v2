# Versioned Macro Calendar

`config/macro_calendar.yaml` is the reviewed local source artifact for U.S. macro
calendar context. Runtime code reads this file only; it does not fetch, scrape,
poll, use browser automation, or depend on external websites or APIs.

## Supported Families

- `FOMC_DECISION` from the Federal Reserve
- `CPI`, `PPI`, `EMPLOYMENT_SITUATION`, and `JOLTS` from BLS
- `PERSONAL_INCOME_AND_OUTLAYS` and `GDP` from BEA
- `RETAIL_SALES` from the U.S. Census Bureau
- `ISM_MANUFACTURING_PMI` and `ISM_SERVICES_PMI` from ISM
- `INITIAL_JOBLESS_CLAIMS` from the U.S. Department of Labor

EIA petroleum releases are excluded because the EIA collector owns that logic.
Earnings, guidance, investor days, Fed speeches, political/geopolitical events,
FDA events, and M&A events are out of scope. Exchange holidays and early closes
are deferred to a separate exchange/session-calendar PR, which is required
before paper trading.

## Artifact Rules

All timestamp fields are canonical offset-aware UTC ISO strings using `Z` or
`+00:00`. Source wording such as `8:30 AM ET` is preserved only in
`source_time_text` and is never used for runtime time math. Date-only official
records are not included as active schedule records.

Every event has a stable `logical_occurrence_id` for one real-world occurrence.
It is preserved across schedule revisions when the official occurrence is the
same, even if the scheduled timestamp changes. `calendar_event_id` is
revision-specific and deterministic from `calendar_version`,
`logical_occurrence_id`, and `schedule_revision_id`.

Rows sharing a `logical_occurrence_id` form one revision chain. Within that
chain, `schedule_captured_at` defines precedence: only the latest captured
revision is operationally active for runtime helpers and collection. Older rows
remain in the raw artifact for audit history.

The global cache key is exactly:

```text
macro_calendar:active:<logical_occurrence_id>
```

The context indicator name is exactly:

```text
macro_calendar_active:<event_type>:<logical_occurrence_id>
```

## Windows

The initial profiles are research hypotheses only:

- `TIER_1`: 10 minutes before, 15 minutes after
- `TIER_2`: 5 minutes before, 10 minutes after
- `TIER_3`: 2 minutes before, 5 minutes after

Active-window behavior is inclusive:

```text
effective_from <= evaluation_time <= valid_until
```

For range listing only, `events_between(start, end)` uses `[start, end)` on
`scheduled_at` so paginated reads do not duplicate boundary events.

## Runtime Output

The collector resolves each revision chain first, then writes currently active
latest revisions into `ContextStateCache` with `value=True`, `severity="INFO"`,
and `source="macro_calendar_v1"`. It emits optional `ContextIndicatorSnapshot`
rows only when an active cache entry is written or replaced. Repeated calls for
the same active event are duplicates and do not write another ledger row.

If the latest revision is `CANCELLED` or `SUPERSEDED`, it revokes any stale
active cache entry for that logical occurrence. If a latest confirmed/tentative
revision is future-dated, it revokes stale cache state from an older revision
while waiting for normal active-window timing.

The details use:

```text
research_window_kind = MACRO_CALENDAR_RESEARCH_WINDOW
```

This is research metadata only. The calendar has no direct trade, risk, sizing,
model, approval, or execution authority.

## Provenance

Calendar-created details use PR29 provenance. `source_event_time` is
`scheduled_at`, `collected_at` is the reviewed local `schedule_captured_at`, and
`valid_until` matches the derived window end. `available_at` is populated only
when the official source provides a verified schedule publication timestamp.

`research_asof_eligible` is true only for `CONFIRMED` events with a verified
`official_schedule_published_at`. Local review time alone does not prove that a
historical decision could have known the schedule.

## Maintenance

Future schedule changes should be data-only PRs. If the same real-world event is
rescheduled, preserve `logical_occurrence_id` only when it still clearly refers
to the same official occurrence, update `schedule_revision_id`, advance
`schedule_captured_at`, mark the old record `SUPERSEDED`, and add the
replacement record. `CANCELLED` and `SUPERSEDED` latest revisions never become
active; they revoke stale active cache entries for the same logical occurrence.

Run:

```powershell
python scripts/check_macro_calendar.py
```
