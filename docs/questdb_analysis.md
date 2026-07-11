# QuestDB Ledger Analysis

PR 15 adds a small read-only QuestDB ledger reader and basic summary helpers.
PR 14 JSONL fallback is intentionally skipped for now.

The reader uses QuestDB's documented `/exec` GET path with `fmt=json` and only
allows single-statement `SELECT` or `WITH` queries. It rejects semicolons and
obvious write/schema tokens before sending a request. It also checks the encoded
GET URL length before calling QuestDB.

## Scope

PR 15 reads existing local QuestDB bot-ledger tables created by the PR 12
schema and written by PR 13. It does not write rows, modify schema, add JSONL
fallback, train models, tune risk, call Alpaca, call Databento APIs, create
dashboards, or query raw market-data tables.

PR 15 assumes the PR 12 schema already exists. It does not add TTL, retention,
or schema migration changes. TTL and retention policy are deferred to a future
local-storage hardening PR.

## Summaries

The basic summary layer answers small operational questions:

- model signal counts and average confidence
- risk decision approved and blocked counts
- cost estimate counts and average net expected edge
- order and fill counts
- average fill slippage
- trade outcome counts and average PnL or returns
- system health warning/error counts

## Validation

Default offline validation does not require QuestDB:

```powershell
python scripts/check_questdb_analysis.py
```

Required server-laptop validation runs read-only summary queries against local
QuestDB after health, schema, and writer validation:

```powershell
python scripts/check_questdb.py --required
python scripts/check_questdb_writer.py --required
python scripts/check_questdb_analysis.py --required
```

For an existing persistent server, apply the relevant additive migrations
before writer/readback validation. After PR34, use
`db/schema/questdb_pr34_add_phase7_context_ledger.sql` with the pre/post count
procedure in `docs/live_runbook.md`. Do not use the destructive
`scripts/check_questdb_schema.py --apply` reset as a migration path. That reset
is limited to a fresh disposable database.
