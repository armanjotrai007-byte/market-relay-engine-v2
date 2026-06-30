# Context Refresh Coordinator

PR31 adds a deterministic, one-shot, in-memory coordinator for existing context
collectors. It decides whether each registered source is due for one invocation
and records coordinator-level freshness and outcome metadata.

The coordinator does not replace collector behavior. EIA still owns release
windows, numeric report retry timing, source requests, and EIA continuation
state. The macro calendar still owns local artifact loading, calendar
revisions, event-window timing, provenance, and cache/ledger behavior. FRED
still owns observation freshness, USAspending still owns checkpoints and award
revision logic, and yfinance remains development-only with completed-bar
validation and completion grace.

## State And Time

Runtime state is in memory only. PR31 does not persist coordinator state to
QuestDB, JSON files, checkpoints, or any database. Existing source-specific
persistence remains inside the existing collectors.

Every coordinator run requires an explicit timezone-aware `evaluation_time`.
The coordinator does not read the current clock. Cold start with
`runtime_state=None` creates a complete default state for every configured
source. Missing known source state is defaulted; unknown source IDs are rejected
instead of carried forward.

Per-source state distinguishes:

- `last_attempted_at`: most recent coordinator invocation of that source.
- `last_completed_at`: most recent normal adapter return.
- `last_usable_at`: most recent adapter-declared usable context result.
- `last_full_success_at`: most recent explicit `SUCCESS`.
- `consecutive_failure_count`: consecutive `FAILED` outcomes only.
- `consecutive_non_usable_count`: consecutive completed or failed outcomes that
  did not produce usable context.

The field name `last_successful_at` is intentionally not used because it is
ambiguous across partial, usable, and fully successful outcomes.

## Due Timing

A source is due when it has never been attempted or when
`evaluation_time >= next_due_at`. Disabled and skipped sources are not invoked.

Adapters may return a source-specific `next_due_at`. The coordinator uses that
hint only when it is timezone-aware and strictly later than `evaluation_time`.
Missing hints fall back to `fallback_interval_seconds` from
`config/context_refresh.yaml`. Naive, equal, or past hints are rejected with an
`INVALID_NEXT_DUE_HINT` issue and fallback timing is used, preventing a busy
retry loop.

Fallback intervals are only a safety net. They must not flatten source-specific
timing. EIA adapter hints preserve release-window starts, fast retry timing,
delayed retry timing, and next release timing. Macro-calendar hints preserve the
next event `effective_from` boundary or the transition immediately after an
active event's `valid_until`. yfinance hints align to the next completed
five-minute bar plus configured completion grace.

## Statuses

The coordinator preserves meaningful distinctions:

```text
DISABLED
SKIPPED_NOT_DUE
SUCCESS
PARTIAL
STALE
NO_FRESH_DATA
DATA_DELAYED
NO_ACTIVE_EVENTS
SUPERSEDED
FAILED
```

Adapters explicitly map native collector statuses into these values. Unknown
native statuses become `FAILED` with a bounded diagnostic issue; arbitrary
nonfailure strings are not treated as success.

Native collector result objects are preserved only on the current-process
`ContextRefreshSourceOutcome.native_result`. They are never stored in runtime
state. JSON-safe run-result projections omit native internals and include only a
small native result summary.

## Execution Boundary

The coordinator is a library API only. It adds no daemon, sleep loop, background
thread, async scheduler, cron integration, QuestDB health preflight, QuestDB
reader, new table, new writer, retry framework, or decision-loop integration.

Sequential source execution is intentional. A failed source is isolated and does
not prevent later due sources from being attempted. Existing collectors remain
responsible for their own source timeouts, retries, writer behavior, and
fallback behavior.

PR31 has no direct trade, risk, model, AI, or execution authority. It does not
block trades, size positions, change model features, approve orders, or claim
that more frequent context collection improves profitability. Later runner code
must invoke this coordinator outside the live decision path. PR32 remains
responsible for decision-time context assembly.
