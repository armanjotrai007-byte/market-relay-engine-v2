# FRED Treasury-rate collector

PR27 adds a one-shot collector for slow daily Treasury-rate context. Repository
configuration intentionally enables it for explicit collection outside the
per-tick loop. It records evidence for later profitability research; it does not
treat daily FRED observations as an intraday scalp signal or grant them trading
authority.

## Source and bounded request policy

The collector requests exactly `DGS3MO`, `DGS2`, and `DGS10` from the official
`fred/series/observations` endpoint. Every request explicitly uses JSON,
`order_by=observation_date`, descending order, and the configured bounded limit
(20 by default, valid range 4–50). It never relies on FRED's default limit,
paginates, performs a historical backfill, or requests a full series history.

`structured_sources.fred.enabled` is `true` in repository configuration. This
permits an explicit collector invocation; it does not schedule requests or add
FRED to the per-tick loop. The API key is read only from the environment variable
named by `api_key_env`, and only during an enabled request. A separately
constructed disabled `FREDConfig` requires no key and performs no HTTP.

## Facts and units

All facts use global scope. Raw yields are stored in `percent`; the three curve
spreads and three previous-valid-observation changes use `percentage_points`.
The categorical `rate_curve_regime_v1` uses `category` and records only the signs
of 2Y−3M and 10Y−2Y. Zero is positive.

Change indicators are named `*_change_prev_valid_obs`. They subtract the prior
valid numeric observation from the latest valid numeric observation. They are not
called daily changes because weekends, holidays, and missing values can create a
multi-day interval. The raw move is not divided by elapsed calendar days; exact
current/prior dates and interval days are retained for later analysis.

Spreads require current component observations with identical dates. The regime
requires all three current observations to share one date. Raw facts remain usable
when an affected derived fact is suppressed by date misalignment.

## Time, freshness, and research safety

`source_event_time` is UTC midnight on the FRED observation date. This is a
querying and deterministic-identity convention for the economic date—not a claim
that the value was published or available to a trading decision at midnight.

Freshness uses the `America/New_York` calendar. The standard-library `ZoneInfo`
database is preferred; the collector includes the same post-2007 U.S. Eastern DST
rules as a dependency-free Windows fallback because this repository declares no
runtime dependencies. For observation date `D` and
configured maximum age `N`, the observation is current from `D` through `D + N`
inclusive. `valid_until` is 23:59:59.999999 New York time on `D + N`, converted to
UTC. Collection time never extends that deadline.

Every stored fact declares:

- `source_event_time_basis=observation_date_utc_midnight_convention`
- `availability_basis=collector_observed`
- `research_asof_eligible=false`
- `vintage_tracking_mode=current_fred_unpinned_v1`

The current FRED request does not pin a historical vintage. Its realtime request
fields are therefore excluded from IDs and stored provenance. PR27 snapshots must
not be silently joined into historical decision labels as if their exact market
availability were known. A future ALFRED design may add true vintage-aware data.

## Cache and ledger behavior

Cache `updated_at` is the UTC-midnight observation convention, not collection
time. Newer observation dates replace older state, older dates are ignored, and a
same-date value revision replaces the prior value. `first_collected_at` is stable
for an identical semantic fact, while the current run's `checked_at` exists only
on the collection result.

Only cache `WRITTEN` and `REPLACED` snapshots are sent through the existing
QuestDB context-indicator writer. Duplicate, stale, invalid, unavailable, and
ineligible facts do not create ledger rows. This includes the categorical regime.
No new table or QuestDB read is introduced. Idempotency is guaranteed while the
cache lives, and deterministic IDs provide stable record identity; PR27 does not
invent durable cross-process uniqueness enforcement.

## Failure semantics

- `DISABLED`: no credential lookup or HTTP.
- `FAILED`: all requests fail, or all reachable responses lack a usable latest value.
- `STALE`: all three requests succeed and all latest valid values are stale.
- `PARTIAL`: any mixed failure, missing value, current/stale mixture, missing prior,
  date misalignment, suppressed derived fact, or optional ledger failure.
- `SUCCESS`: all three sources are current, complete, aligned, issue-free, and all
  ten facts are produced.

Ignored dots, malformed rows, non-finite values, duplicate dates, and future rows
are selection noise when two usable observations still exist.

## Profitability boundary

PR27 is designed to make later profitability testing possible. It does not claim
that any Treasury yield, spread, change, or regime is profitable. The raw logged
values and spreads remain canonical; the versioned regime is only a convenience
for historical joins and can be recomputed. FRED release-calendar and known macro
event-window work belongs to PR29.

The live trading path remains Databento features, model signal, deterministic risk
gate, and Alpaca paper execution. FRED context does not alter any part of that path.
