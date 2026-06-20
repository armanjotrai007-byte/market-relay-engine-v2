# QuestDB Ledger Schema

PR 12 adds the official QuestDB V2 bot-ledger schema SQL and offline schema
validation.

The schema file is:

```text
db/schema/questdb_ledger_v1.sql
```

PR26 adds `details_json STRING` to `context_indicator_snapshots`. Existing
servers should run the non-destructive one-time migration
`db/schema/questdb_pr26_add_context_indicator_details_json.sql` before enabling
live EIA WPSR ledger writes.

It is a destructive local-development reset. It drops old V1/raw/PDF-era table
names, drops existing V2 ledger tables, and recreates the V2 ledger tables from
scratch. Do not run it against data you need to preserve.

## Ledger Scope

QuestDB remains the bot ledger and black-box recorder only. It records bot runs,
sessions, feature snapshots, model signals, cost estimates, context state,
context events and flags, risk decisions, order and fill events, trade
outcomes, latency metrics, system health, and future ledger write failures.

QuestDB is not a raw historical market-data warehouse. The schema does not
create raw Databento tables such as `raw_trades`, `raw_bbo`, `raw_tbbo`,
`raw_ohlcv`, `raw_mbp10`, or `databento_definitions`.

Historical market truth remains official Databento DBN or Parquet files outside
QuestDB.

## Context State Snapshots

PR 12 resolves the `risk_decisions.context_snapshot_id` ambiguity by adding
`context_state_snapshots`.

`context_state_snapshots` represents the context state seen by the deterministic
risk gate at decision time. A `risk_decisions.context_snapshot_id` value points
to a row in `context_state_snapshots`, so future analysis can query the context
state used for a risk decision.

PR 12 only adds schema. It does not add a context-state writer.

## Validation

Run the default offline schema check:

```powershell
python scripts/check_questdb_schema.py
```

The offline check verifies required table definitions, forbidden raw table
creation, context snapshot linkage, drop-before-create ordering, and the absence
of schema test data or retention clauses.

Run the real server-laptop apply validation only with QuestDB running:

```powershell
python scripts/check_questdb.py --required
python scripts/check_questdb_schema.py --apply --required
```

The apply command uses QuestDB's documented `/exec` GET endpoint, sends one SQL
statement at a time with `fmt=json`, fails fast on errors, and verifies the
expected tables exist afterward.

If schema apply fails mid-execution, the reset is destructive and may leave a
partial schema. For local development, rerun the same schema apply command from
scratch as the recovery path.

## Not Added

PR 12 does not add:

- runtime ledger writers
- app insert logic
- JSONL fallback implementation
- raw Databento market-data tables
- historical market warehouse behavior
- Databento API calls
- Alpaca or broker execution
- model training or inference
- risk engine logic
- live trading

Next PR: PR 13 - QuestDB Ledger Writer.
