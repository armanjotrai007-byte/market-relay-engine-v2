# QuestDB Health Check

QuestDB is the bot ledger and black-box recorder. It stores bot facts such as
signals, context metadata, risk decisions, orders, fills, outcomes, latency,
slippage, health, classification attempts, and shadow evaluations. It is not a
historical market-data warehouse or the per-tick/per-signal context source.
Raw Databento trades, BBO/MBP/OHLCV, and historical market truth remain in
official local Databento DBN/Parquet-derived files.

## Local defaults

```text
QUESTDB_HTTP_SCHEME=http
QUESTDB_HTTP_HOST=localhost
QUESTDB_HTTP_PORT=9000
QUESTDB_HEALTH_TIMEOUT_SECONDS=3
QUESTDB_PG_HOST=localhost
QUESTDB_PG_PORT=8812
```

Configuration precedence is explicit overrides, then environment/local `.env`,
then `config/questdb.yaml`, then hard-coded defaults. Secrets must never be
printed or committed.

## Health check

Config-driven mode (required by current repository configuration):

```powershell
& ".\.venv\Scripts\python.exe" scripts/check_questdb.py
```

Explicit server-laptop required mode:

```powershell
& ".\.venv\Scripts\python.exe" scripts/check_questdb.py --required
```

The health check sends only `SELECT 1` to the HTTP `/exec` SQL endpoint so it
verifies the SQL query engine needed by ledger components.
`[SKIP]` and `[WARN]` are possible only when a caller or
`QUESTDB_HEALTH_REQUIRED=false` explicitly selects optional behavior; `[PASS]`
means the SQL engine accepted the read-only query. Current config uses
`health_check.required_by_default: true`.

## PR34 safety boundary

PR34 validation is offline. Its one authorized preflight inspection used only
read-only version, table-column, and row-count queries; no schema or row was
mutated. Applying the persistent-ledger upgrade is a deliberate operator action
after merge, with writers stopped and pre/post row counts recorded.

Use `db/schema/questdb_pr34_add_phase7_context_ledger.sql` for that additive,
idempotent upgrade. Never use `db/schema/questdb_ledger_v1.sql` or
`scripts/check_questdb_schema.py --apply` as a migration path for a ledger whose
rows must be preserved. The full procedure is in `docs/live_runbook.md` and
`docs/questdb_schema.md`.

`risk_decisions.context_snapshot_id` continues to reference the decision-time
`context_state_snapshots.context_snapshot_id` ledger record. PR34 adds separate
research-only classification and shadow metadata tables and does not change
that real decision-context linkage.
