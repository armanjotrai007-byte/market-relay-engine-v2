# Context State Cache

## Purpose

PR24 adds a bounded in-memory `ContextStateCache` for the latest structured context facts. It is the fast live-decision memory that future risk logic can read without using QuestDB, external services, AI calls, or model inference during a trade decision.

The cache stores the latest facts only. QuestDB remains the ledger and history store, not the hot-path source for live context reads.

## Scope

Context entries use one of three scopes:

- `GLOBAL`: broad market, macro, calendar, or regime facts.
- `TICKER`: ticker-specific facts such as earnings risk for `AAPL`.
- `SECTOR`: sector or proxy facts such as `TECH` sector weakness.

Future sector proxy collectors should write sector facts under `SECTOR` scope. They should not invent a separate sector-proxy convention.

## Entry Times

`updated_at` is when the cache entry was written or refreshed. Helper constructors require it explicitly so tests and future collectors remain deterministic.

`source_event_time` is optional and represents the timestamp of the underlying source data or event when the source has one.

`valid_until` is optional and represents an independent absolute deadline after which the cached context should stop being trusted. It is not ordered against `updated_at`; a delayed collector may write an entry whose `valid_until` is already in the past, equal to `updated_at`, or after `updated_at`.

The cache preserves collector-supplied `updated_at`, `source_event_time`, and `valid_until` after UTC normalization. It never rewrites `valid_until`, extends expired validity, or replaces it with `updated_at`.

Expired entries are accepted so stale context can be surfaced conservatively through `to_context_state_snapshot(...)`. Expired entries are hidden by default from normal reads and raw cache snapshots unless `include_expired=True` is requested.

An entry is expired only when `now > valid_until`. At `now == valid_until`, the entry is still visible.

## Update Results

`update(...)` returns `ContextStateUpdateResult` instead of raising for normal race outcomes:

- `WRITTEN`: first active value for a key.
- `REPLACED`: newer value, or same timestamp with changed value, severity, source, or details.
- `IGNORED_STALE`: older value for an existing key.
- `IGNORED_DUPLICATE`: same timestamp with unchanged value, severity, source, and details, including metadata-only changes.

Invalid local inputs still raise `ContextStateCacheError`.

## Memory Bound

The cache is bounded by `max_entries`, defaulting to `10000`. `purge_every_updates`, defaulting to `1000`, controls periodic expired-entry purging during accepted update attempts.

If the cache is still above `max_entries` after an insert or replacement, it evicts oldest entries by `updated_at`. Ties are deterministic using context key sort order.

This is still a simple in-process memory cache. It does not start a background worker.

## JSON Isolation

Entry `details` must be JSON-safe. Details are deep-copied during `ContextStateEntry` construction, before entries are stored internally, and when read APIs return entries. Snapshots also return deep-copied nested data. Mutating the original details object, a returned entry, or a returned snapshot cannot mutate cache internals.

`snapshot()` returns a plain JSON-safe dictionary with this structure:

```json
{
  "global": {},
  "tickers": {},
  "sectors": {},
  "entry_count": 0
}
```

Entry dictionaries include scope, name, ticker, sector, value, severity, source, `updated_at`, `source_event_time`, `valid_until`, confidence, details, trace ID, and whether the entry is expired.

## ContextStateSnapshot Bridge

The existing risk adapter consumes the `ContextStateSnapshot` contract. PR24 adds `to_context_state_snapshot(...)` to aggregate relevant cache entries into that contract without calling the risk filter.

For a requested ticker, aggregation can include:

- global entries
- ticker entries for that ticker
- sector entries for the provided sector

Fresh relevant entries aggregate normally into the active summary. Expired relevant entries are reported separately whenever they are present; they are not treated as fresh and do not appear in the active grouped global, ticker, or sector entries.

When expired relevant entries are present, `context_summary` includes `fresh_entry_count`, `expired_entry_count`, `expired_context_present`, `stale_context_policy="ELEVATED"`, and JSON-safe expired entry metadata in `expired_entries`.

Relevant expired context imposes a minimum effective risk of `ELEVATED` even if other fresh entries are only low-risk or informational. If fresh risk is absent or `LOW`, the snapshot returns `risk_level="ELEVATED"` and `highest_severity="EXPIRED"`. If fresh risk is already `ELEVATED`, the snapshot keeps `risk_level="ELEVATED"` and preserves the real fresh highest severity. Fresh `HIGH` risk remains `HIGH` and keeps the real fresh highest severity.

If no relevant entries exist at all, `risk_level` remains `None` and expired context fields are absent.

The method calculates fresh `highest_severity`, maps severity to `risk_level`, sets `valid_until` to the earliest included non-null validity time for fresh entries only, and stores JSON-safe grouped entries in `context_summary`.

Risk level mapping for fresh entries:

- `CRITICAL` or `HIGH`: `HIGH`
- `MEDIUM`: `ELEVATED`
- `LOW`: `LOW`
- `INFO` or no entries: `None`

PR24 does not integrate this bridge into `evaluate_risk(...)`. A future PR can wire it into live risk decisions.

## Thread Safety

Public cache methods are protected by an internal `threading.RLock` for basic in-process concurrent reads and writes.

The cache is not cross-process shared state and is not durable storage.

## Not Included

PR24 does not add:

- collectors
- external API calls
- AI calls
- model inference
- QuestDB reads
- QuestDB writes
- order submission
- Alpaca calls
- background services
- schedulers
- retries
- new heavy dependencies
