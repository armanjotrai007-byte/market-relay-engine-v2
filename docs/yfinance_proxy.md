# YFinance Development Proxy Collector

PR25 adds a development-only collector that uses `yfinance` as a simple proxy
source for broad-market and sector context indicators. Repository configuration
intentionally enables it for explicit connectivity and cache/ledger validation,
but it remains non-required and outside the per-tick loop. It is not production
market truth, not a trading signal, and not a substitute for official market
data.

## Purpose

The collector exists to exercise the PR24 `ContextStateCache` and optional QuestDB context-indicator ledger path with real-shaped numeric context values. It is intentionally one-shot and manually invoked. It does not run in the per-tick loop, does not schedule itself, and does not submit orders.

## Configuration

The source lives at `structured_sources.yfinance_dev_only` in `config/context_sources.yaml`.

Current repository settings remain development-safe:

```yaml
enabled: true
development_only: true
production_critical: false
feeds_memory_cache: true
writes_questdb_ledger: true
used_in_per_tick_loop: false
required: false
period: "5d"
interval: "5m"
timeout_seconds: 10.0
bar_completion_grace_seconds: 30
max_staleness_seconds: 360
auto_adjust: false
actions: false
repair: false
keepna: true
prepost: false
threads: true
```

PR25 supports only five-minute bars. Validation rejects any interval except `5m` and requires:

```text
max_staleness_seconds >= 300 + bar_completion_grace_seconds
```

Equality is valid. With the default 30 second grace, `max_staleness_seconds=330` is the minimum valid value and `360` is the default. The collector sets:

```text
valid_until = source_event_time + max_staleness_seconds
```

Validity is not calculated from collection time. This prevents a rolling stale-data blackout after each five-minute boundary: while the newest bar is still inside its completion grace window, the previous completed bar remains valid through that grace period.

## Symbols And Scopes

Collected symbols are fixed for PR25:

```text
SPY, QQQ, IWM, GLD, ^VIX, XLE, XOP, OIH, XLI, PPA, ITA
```

Scope mapping:

```text
SPY, QQQ, IWM, GLD, ^VIX -> GLOBAL
XLE, XOP, OIH -> SECTOR / OIL
XLI -> SECTOR / INDUSTRIALS
PPA, ITA -> SECTOR / DEFENSE
```

`OIL` matches the configured tradable sector value used by the final oil
universe (`XOM`, `OXY`, `SLB`, `COP`, and `VLO`) after PR24 sector normalization.
Calling `get_sector_proxy_indicators(..., sector="oil", ...)` and
`get_sector_proxy_indicators(..., sector="OIL", ...)` resolves the same
XLE/XOP/OIH readings.

Sector proxies use the existing PR24 `SECTOR` scope. They do not create ticker keys and do not create keys that contain both ticker and sector. Cache entry names include the source proxy symbol, for example:

```text
yfinance_dev_raw_v1:XLE:return_5m:5m
yfinance_dev_raw_v1:XOP:return_5m:5m
```

That allows multiple ETFs to coexist under the same sector.

## Collection Flow

The collector performs one efficient batch request, then retries only affected missing or ambiguous symbols individually once:

```text
one batch download
-> inspect and normalize batch shape
-> identify missing or ambiguous symbols
-> retry only affected symbols individually once
-> create one final normalized dataframe per symbol
-> remove incomplete bars
-> validate freshness
-> calculate indicators
-> publish cache entries
-> optionally write ledger rows
```

It never publishes from a partial batch before fallback processing completes, and it does not write the same symbol twice in one run.

## Data Handling

Supported yfinance dataframe layouts:

- two-level columns with price and ticker levels in either order
- one-level columns only when exactly one symbol was requested

The index must be timezone-aware. Timestamps are converted to UTC and sorted ascending. Duplicate timestamps with identical close values are collapsed; duplicate timestamps with conflicting close values invalidate that symbol.

Bar timestamps are treated as five-minute bar starts. A bar is complete only when:

```text
collection_time >= bar_start + 5 minutes + bar_completion_grace_seconds
```

All incomplete or future rows are removed, not just the last row.

## Indicators

PR25 emits only:

```text
latest_close
return_5m
return_15m
return_60m
```

Return windows require exact timestamp matches. There is no nearest timestamp fallback, row-count fallback, forward fill, overnight fallback, or prior-session fallback.

Before calculating a return, both closes must be independently valid:

```text
latest_close is finite and > 0.0
target_close is finite and > 0.0
```

If the target timestamp exists but its close is invalid, only that return is omitted. The collector records `INVALID_TARGET_CLOSE`, does not publish `None`, does not update that cache key, and continues other valid windows.

## Cache And Numeric Retrieval

All PR25 cache entries use:

```text
source = yfinance_dev_raw_v1
severity = INFO
```

Negative returns and volatility-like values are not converted into risk flags in PR25. The existing risk adapter consumes severity/risk classifications and will ignore these `INFO` entries until a later deterministic numeric-rule PR consumes the retrieval contract.

`ContextStateEntry.value` now accepts JSON-safe scalar values: non-empty strings, integers, finite floats, and booleans. Structured metadata remains in `details`.

Read-only helpers expose finite numeric proxy values without QuestDB reads or risk thresholds:

```python
get_proxy_indicator(cache, registry, *, symbol, indicator_name, window, now=None, include_expired=False)
get_sector_proxy_indicators(cache, registry, *, sector, indicator_name, window, now=None, include_expired=False)
```

By default, expired readings are hidden. `include_expired=True` returns expired readings for explicit inspection.

## Deterministic IDs

PR25 records pass explicit deterministic `context_indicator_id` values. The identity payload is canonical JSON built from:

```text
[source, symbol, indicator_name, window, source_event_time_iso]
```

It is encoded with `ensure_ascii=True` and compact separators, hashed with SHA-256, and stored as:

```text
context_indicator_<first 32 hex chars>
```

Collection time, trace ID, run ID, session ID, and process restart do not affect the ID. A different completed bar produces a different ID.

`ContextIndicatorSnapshot` still supports existing callers that do not pass an ID; those callers receive a generated default ID. The QuestDB converter preserves `record.context_indicator_id` and does not generate a replacement ID.

## QuestDB Publication

Cache publication happens first. QuestDB rows are written only for cache updates that return `WRITTEN` or `REPLACED`. Duplicate and stale cache updates do not write ledger rows.

The collector depends on a small protocol, not directly on the concrete writer:

```python
write_context_indicator_snapshot(snapshot, **kwargs) -> object | None
```

Optional writer failures become structured issues and do not roll back cache updates. Required writer failures raise a clear `YFinanceProxyError`.

When the live smoke is run with `--write-questdb`, the script treats QuestDB writes as required. It exits nonzero if any ledger write fails, if a `LEDGER_WRITE_FAILED` issue is present, or if valid indicators are produced but zero QuestDB rows are successfully written. A `PARTIAL` result caused only by unrelated symbol-data issues can still pass when at least one valid indicator is successfully written and no ledger write fails.

## Result Statuses

- `DISABLED`: disabled and made no source or database calls.
- `SUCCESS`: every requested symbol produced all expected indicators without issues.
- `PARTIAL`: at least one indicator was published, but some symbol, window, validation, or write produced issues.
- `NO_FRESH_DATA`: source request and response processing worked, but nothing was published because bars were stale, incomplete, likely after-hours, or otherwise no fresh completed observations existed without source/schema failure.
- `FAILED`: nothing was published because source download, schema normalization, timestamps, or all requested symbols failed structurally.

The collector does not assert the market is definitely closed because PR25 intentionally does not add an exchange-calendar dependency.

## Checks

Offline check, no internet and no QuestDB:

```powershell
python scripts/check_yfinance_proxy.py
```

Optional live smoke:

```powershell
python scripts/check_yfinance_proxy.py --live
```

If live yfinance is reachable but no fresh completed bars are available, `NO_FRESH_DATA` prints a warning and exits successfully by default. To require fresh bars:

```powershell
python scripts/check_yfinance_proxy.py --live --require-fresh
```

To write live successful indicators through the configured QuestDB writer and require those writes to succeed:

```powershell
python scripts/check_yfinance_proxy.py --live --write-questdb
```

`--write-questdb` is rejected unless `--live` is also present.

## Explicit Exclusions

No order submission, Alpaca integration, Databento integration, AI/model calls, strategy thresholds, deterministic risk approval changes, position sizing, QuestDB reads, new QuestDB tables, background service, scheduler, queue, web server, dashboard, or generic collector framework are added in PR25.
