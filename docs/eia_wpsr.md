# EIA Weekly Petroleum Status Report

PR26 adds a disabled-by-default, one-shot EIA WPSR collector for oil-market
research context. It does not create a scheduler, trading signal, directional
label, inventory-surprise model, position-sizing rule, or numeric risk limit.

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

## Reviewed runtime schedule

`config/calendar_events.yaml` is runtime authority. It remains disabled and has
no tracked real-world releases by default. Each reviewed release has:

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
existing deterministic risk adapter. Numeric collection begins only after the
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
enabling EIA ledger writes.

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
