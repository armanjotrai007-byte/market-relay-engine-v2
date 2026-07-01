# Decision-Time Context Assembly

PR32 adds a deterministic read-side projection for one ticker at one explicit
evaluation time. It answers what structured context was visible to the process
for that ticker without deciding whether to trade, sizing a position, calling a
model, or invoking risk rules.

## Ownership Boundary

`ContextStateCache` remains the in-memory owner of latest structured entries.
PR31 remains the owner of source refresh runtime state. Collectors remain the
owners of source-specific collection and cache materialization. Existing
`ContextStateSnapshot` remains unchanged for the current risk adapter.

`DecisionContextAssembler` does not invoke collectors, call the PR31
coordinator, write cache state, load config files, read or write QuestDB, create
network clients, or persist audit payloads. It performs one cache snapshot read
and then filters that returned JSON-safe snapshot in memory.

## Selection

Assembly requires a ticker, trace ID, timezone-aware evaluation time, and an
optional PR31 runtime state. The ticker and sector values use the existing
uppercase normalization convention.

Sector resolution is explicit and recorded:

- `EXPLICIT`: `ticker_sector` was provided to `build_for_decision`.
- `INJECTED_MAPPING`: the assembler's `ticker_sector_by_ticker` mapping supplied
  the sector.
- `UNRESOLVED`: no sector was available.

Global entries are always eligible. Ticker entries are selected only when their
normalized ticker exactly matches the requested ticker. Sector entries are
selected only when a sector was resolved and exactly matches the entry sector.
When sector resolution is unresolved, sector entries are excluded and no sector
is guessed or loaded from symbol configuration.

The cache snapshot is not a historical event store. PR32 excludes entries whose
`updated_at` is after the requested evaluation time and reports only
`future_entry_exclusion_count`. It does not reconstruct prior values that may
have been replaced in the cache after the evaluation time. Historical replay
requires future ledger or vintage work.

## Labels

Every selected cache entry is projected into `DecisionContextEntry` with its
source-specific `details` preserved. PR32 adds only standard labels:

- `resource_family`: `MACRO_CALENDAR`, `EIA_WPSR`, `FRED`, `USASPENDING`,
  `YFINANCE_DEV`, or `UNKNOWN`.
- `source_mode`: `LOCAL_REVIEWED`, `OFFICIAL_SOURCE`, `DEVELOPMENT_ONLY`, or
  `UNKNOWN`.
- `selection_scope`: `GLOBAL`, `SECTOR_MATCH`, or `TICKER_MATCH`.
- `authority_class`: `RESEARCH_ONLY`, `DEVELOPMENT_ONLY`, or
  `APPROVED_RISK_CONTEXT`.
- `provenance_state`: `ASOF_ELIGIBLE`, `ASOF_INELIGIBLE`, or
  `MISSING_OR_MALFORMED`.
- `refresh_status`: the as-of-safe PR31 source status or an explicit unknown
  status.

Unknown source names remain visible with `UNKNOWN` labels and
`RESEARCH_ONLY` authority. Source-specific evidence stays inside `details`; PR32
does not duplicate collector schemas into a second abstraction layer.

## Provenance And Readiness

Provenance uses the PR29 safe extraction helpers. A record is `ASOF_ELIGIBLE`
only when valid provenance exists and the historical eligibility helper returns
true for the evaluation time. Valid but ineligible provenance is
`ASOF_INELIGIBLE`; missing or malformed provenance is
`MISSING_OR_MALFORMED` and never crashes assembly.

Source readiness emits one record for each PR31 source ID: `macro_calendar`,
`eia_wpsr`, `fred`, `usaspending`, and `yfinance_dev_only`. Missing runtime
state becomes `UNKNOWN_NOT_REFRESHED`.

Readiness is protected against future-state leakage. Refresh statuses are exposed
only when their explicit `last_status_observed_at` time is at or before the
decision evaluation time. Otherwise source readiness degrades to canonical
`UNKNOWN_NOT_REFRESHED` rather than leaking a later coordinator outcome.
`readiness_age_seconds` is calculated only from an as-of-compatible
`last_completed_at`.

## Policy Boundary

`DecisionContextPolicy` is default-deny. `all_structured_context` always contains
all selected visible cache entries. `approved_risk_context` is empty unless an
injected policy exactly matches `source`, `cache_scope`, and `cache_name`.

Approval is never inferred from severity, source-name substrings, event tiers,
flag types, or provenance state. Approved entries are labelled
`APPROVED_RISK_CONTEXT` for future consumers, but PR32 does not route them to the
risk filter or change any trade outcome.

`context_snapshot_id` is deterministic and is the future join value intended for
the existing `RiskDecision.context_snapshot_id` field. PR32 does not modify
`RiskDecision`, risk logging, risk rules, orders, execution, AI, or model code.

## Deferred Work

The audit payload is returned by `DecisionContext.to_audit_payload()` and is
JSON-safe, but PR32 does not persist it to QuestDB, JSONL, disk, or any external
system. Runner persistence, historical replay, risk-policy integration, and raw
`ContextFlag` or `ContextAIEvent` assembly are deferred to later reviewed work.
