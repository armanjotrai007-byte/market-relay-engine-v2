# EIA Weekly Petroleum Status Report

PR26 adds a one-shot EIA WPSR collector for oil-market research context.
Repository configuration intentionally enables it for explicit collection, but
does not schedule or invoke it automatically. It does not create a trading
signal, directional label, inventory-surprise model, position-sizing rule, or
numeric risk limit.

## Timing and look-ahead control

Every reviewed release contains an offset-aware `release_at` and its exact
`report_period`. The report period describes the inventory week; it is not the
time when the market could observe the data. Numeric snapshots therefore use
the reviewed release time as `source_event_time` and keep the observed period
in `details`. Treating the earlier report period as availability time would
introduce look-ahead bias in later Databento event studies.

The official API records expose weekly periods, series/facet identity, units,
and values. They do not expose a publication timestamp or distinguish revisions
as separate versions. PR26 therefore supports prospective reviewed release
alignment only and does not guess historical publication times.

PR34 makes availability explicit without changing EIA behavior. For each
release-window `ContextFlag`, both top-level `available_at` and
`details["provenance"]["available_at"]` use the reviewed official `release_at`.
They mean the earliest trusted demonstrable public availability of the report.
The pre-release `event_time`/window start remains earlier by design and is not
treated as publication. The adapter validates both availability representations
and rejects a mismatch or malformed nested timestamp.

## Reviewed runtime schedule

`config/calendar_events.yaml` is runtime authority. PR33 may commit reviewed
runtime release windows for functional context-source validation. Each reviewed
release has:

```yaml
- release_id: eia_wpsr_YYYY_MM_DD
  release_at: "2026-01-01T12:00:00-05:00"
  report_period: "2025-12-26"
```

`python scripts/refresh_eia_wpsr_schedule.py --live` reads the official EIA
schedule, combines the normal Wednesday rule with listed holiday exceptions,
and emits review candidates. It never edits runtime configuration.

## Release protection and action planning

The configured release window is inclusive and defaults to five minutes before
through fifteen minutes after release. A pure planner accepts explicit
timezone-aware `evaluation_time`; it never reads a clock, sleeps, loops, or
schedules work. It returns one release-window, numeric-fetch, retry, or no-op
action plus the next action time.

Ticker-level `eia_wpsr_event_window` flags are the only PR26 inputs to the
existing deterministic risk adapter. The final configured oil universe is
`XOM`, `OXY`, `SLB`, `COP`, and `VLO`. Numeric collection begins only after the
configured delay and stops an unfinished cycle at the next release window with
`SUPERSEDED`.

PR26 defines correct action timing and one-shot collection behavior. It does
not make EIA protection automatic. A later runtime-orchestration PR must call
the planner at `next_action_at` and resolve each ticker's sector from
`config/symbols.yaml` before calling the existing cache snapshot builder.

## Numeric metrics and scope

The collector uses `/v2/petroleum/stoc/wstk/data/` for four stock metrics and
`/v2/petroleum/pnp/wiup/data/` for refinery utilization. It requires the exact
reviewed current period and computes WoW only from the record exactly seven
calendar days earlier.

All five levels and five changes are written once under the existing
`SECTOR/OIL` scope. They are not replicated for tickers or ETFs and do not
affect trade approval. Numeric validity ends at the next reviewed release under
the cache's existing inclusive boundary semantics.

## Cache, QuestDB, and validation

The cache is updated before the ledger. QuestDB writes occur only after
`WRITTEN` or `REPLACED`; QuestDB is never read by collection or risk logic.

PR26 adds `details_json` to `context_indicator_snapshots`. Existing servers must
run `db/schema/questdb_pr26_add_context_indicator_details_json.sql` once before
enabling EIA ledger writes. After PR34 is merged, persistent ledgers must also
run `db/schema/questdb_pr34_add_phase7_context_ledger.sql` before PR34 writers;
the destructive reset is not a migration path.

Offline validation uses sanitized fixtures:

```powershell
python scripts/check_eia_wpsr.py
python -m pytest tests/unit/test_eia_wpsr.py -v
```

Read-only live validation:

```powershell
python scripts/inspect_eia_wpsr.py --live
python scripts/refresh_eia_wpsr_schedule.py --live
python scripts/check_eia_wpsr.py --live
```

The API key is read only from `EIA_API_KEY`. Scripts never print the key,
headers, or credential-bearing URLs. Work-laptop validation never writes
QuestDB. Future work includes historical alignment, consensus data, Databento
event studies, and only then validated thresholds or signal rules.
